import base64
import json
import logging
import subprocess
import sys
import tempfile
import re
from collections import defaultdict
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import AsyncOpenAI
import pdfplumber
import pypdfium2 as pdfium
import requests

from app.common.psycopg_pool_conf import psycopg_pool
from app.core.config import settings
from app.repositories.policy_document_repository import PolicyDocumentRepository
from app.schemas.policy_document_schema import (
    PolicyDocumentChunkIngestItem,
    PolicyDocumentChunkIngestResponse,
    PolicyDocumentChunkSkipItem,
    PolicyReferenceDocumentIngestItem,
    PolicyReferenceDocumentIngestResponse,
    PolicyReferenceDocumentSkipItem,
)

try:
    import fitz
except ImportError:
    fitz = None


POLICY_REFERENCE_VISION_MODEL = "gpt-4o"
POLICY_REFERENCE_OPENAI_MAX_BYTES = 50 * 1024 * 1024
POLICY_REFERENCE_SECTION_TARGET_CHARS = 1800

REFERENCE_HEADER_PATTERN = re.compile(
    r"^\s*(?:"
    r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.\s]+.+|"
    r"\d{1,2}[.)]\s+.+|"
    r"\d{1,2}\.\d{1,2}[.)]?\s+.+|"
    r"[가-하][.)]\s+.+|"
    r"제\s*\d+\s*[장절]\s+.+"
    r")$"
)
REFERENCE_HEADER_KEYWORDS = (
    "개요",
    "목적",
    "근거",
    "현황",
    "방향",
    "추진",
    "대상",
    "기준",
    "선정",
    "내용",
    "지원",
    "급여",
    "서비스",
    "신청",
    "접수",
    "방법",
    "절차",
    "서류",
    "제출",
    "유의",
    "주의",
    "제외",
    "제한",
)


@dataclass
class PdfExtractionResult:
    method: str
    text: str
    quality_score: float
    metrics: dict[str, Any] = field(default_factory=dict)


class PolicyDocumentService:
    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.PolicyDocumentService")

    async def ingest_policy_detail_chunks(
        self,
        limit: int = 10,
        rebuild: bool = False,
    ) -> PolicyDocumentChunkIngestResponse:
        items: list[PolicyDocumentChunkIngestItem] = []
        skipped: list[PolicyDocumentChunkSkipItem] = []
        failed: list[dict[str, str]] = []

        async with psycopg_pool.connection() as conn:
            sources = await PolicyDocumentRepository.find_policy_detail_sources(
                conn,
                limit,
                rebuild=rebuild,
            )

            for source in sources:
                try:
                    raw_text, section_documents = self._build_policy_detail_documents(
                        source
                    )
                    if not raw_text:
                        skipped.append(
                            PolicyDocumentChunkSkipItem(
                                policy_id=source["policy_id"],
                                policy_code=source["policy_code"],
                                policy_name=source["policy_name"],
                                reason="chunk로 만들 정책 상세 텍스트가 없습니다.",
                            )
                        )
                        continue

                    chunk_documents = self.split_policy_detail_documents(
                        section_documents
                    )
                    async with conn.transaction():
                        document_id = (
                            await PolicyDocumentRepository.upsert_policy_detail_document(
                                conn=conn,
                                policy_id=source["policy_id"],
                                condition_profile_id=source.get(
                                    "condition_profile_id"
                                ),
                                source_title=self._source_title(source),
                                source_url=source.get("official_url"),
                                raw_text=raw_text,
                            )
                        )
                        deleted_embedding_count = (
                            await PolicyDocumentRepository.delete_policy_detail_embeddings_for_document(
                                conn=conn,
                                document_id=document_id,
                            )
                        )
                        chunk_count = (
                            await PolicyDocumentRepository.replace_document_chunks(
                                conn=conn,
                                document_id=document_id,
                                chunk_documents=chunk_documents,
                            )
                        )

                    items.append(
                        PolicyDocumentChunkIngestItem(
                            policy_id=source["policy_id"],
                            condition_profile_id=source.get("condition_profile_id"),
                            document_id=document_id,
                            policy_code=source["policy_code"],
                            policy_name=source["policy_name"],
                            raw_text_length=len(raw_text),
                            chunk_count=chunk_count,
                            deleted_embedding_count=deleted_embedding_count,
                        )
                    )
                except Exception as exc:
                    self.logger.exception("정책 상세 chunk 생성 중 오류 발생")
                    failed.append(
                        {
                            "policy_id": str(source.get("policy_id")),
                            "policy_code": str(source.get("policy_code")),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

        return PolicyDocumentChunkIngestResponse(
            requested_count=len(sources),
            completed_count=len(items),
            skipped_count=len(skipped),
            failed_count=len(failed),
            items=items,
            skipped=skipped,
            failed=failed,
        )

    async def ingest_policy_reference_documents(
        self,
        limit: int = 10,
        rebuild: bool = False,
    ) -> PolicyReferenceDocumentIngestResponse:
        items: list[PolicyReferenceDocumentIngestItem] = []
        skipped: list[PolicyReferenceDocumentSkipItem] = []
        failed: list[dict[str, str]] = []

        async with psycopg_pool.connection() as conn:
            targets = await PolicyDocumentRepository.find_policy_reference_download_targets(
                conn=conn,
                limit=limit,
                rebuild=rebuild,
            )
            targets_by_url = self._group_by_source_url(targets)

            for source_url, documents in targets_by_url.items():
                try:
                    downloaded = self._download_reference_document(source_url)
                    file_type = self._detect_file_type(
                        content=downloaded["content"],
                        source_title=documents[0]["source_title"],
                        content_type=downloaded.get("content_type"),
                    )
                    extraction_result = self._extract_text(
                        content=downloaded["content"],
                        file_type=file_type,
                    )
                    raw_text = extraction_result.text
                    if not raw_text:
                        for document in documents:
                            skipped.append(
                                self._reference_skip_item(
                                    document=document,
                                    reason="추출된 텍스트가 없습니다.",
                                )
                            )
                        continue

                    for document in documents:
                        chunk_documents = self.split_reference_documents(
                            self._build_policy_reference_documents(
                                source=document,
                                raw_text=raw_text,
                                file_type=file_type,
                                extraction_result=extraction_result,
                            )
                        )
                        async with conn.transaction():
                            await PolicyDocumentRepository.update_document_raw_text(
                                conn=conn,
                                document_id=document["document_id"],
                                raw_text=raw_text,
                            )
                            deleted_embedding_count = (
                                await PolicyDocumentRepository.delete_policy_reference_embeddings_for_document(
                                    conn=conn,
                                    document_id=document["document_id"],
                                )
                            )
                            chunk_count = (
                                await PolicyDocumentRepository.replace_document_chunks(
                                    conn=conn,
                                    document_id=document["document_id"],
                                    chunk_documents=chunk_documents,
                                )
                            )

                        items.append(
                            PolicyReferenceDocumentIngestItem(
                                document_id=document["document_id"],
                                policy_id=document["policy_id"],
                                policy_code=document["policy_code"],
                                policy_name=document["policy_name"],
                                source_title=document["source_title"],
                                source_url=document["source_url"],
                                file_type=file_type,
                                raw_text_length=len(raw_text),
                                chunk_count=chunk_count,
                                extraction_method=extraction_result.method,
                                extraction_quality_score=(
                                    extraction_result.quality_score
                                ),
                                deleted_embedding_count=deleted_embedding_count,
                            )
                        )
                except ValueError as exc:
                    for document in documents:
                        await PolicyDocumentRepository.mark_document_ingest_failed(
                            conn=conn,
                            document_id=document["document_id"],
                            error=str(exc),
                        )
                        skipped.append(
                            self._reference_skip_item(
                                document=document,
                                reason=str(exc),
                            )
                        )
                except Exception as exc:
                    self.logger.exception("정책 관련 문서 처리 중 오류 발생")
                    for document in documents:
                        await PolicyDocumentRepository.mark_document_ingest_failed(
                            conn=conn,
                            document_id=document["document_id"],
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    failed.append(
                        {
                            "source_url": source_url,
                            "document_ids": ",".join(
                                str(document["document_id"]) for document in documents
                            ),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

        return PolicyReferenceDocumentIngestResponse(
            requested_url_count=len(targets_by_url),
            completed_count=len(items),
            skipped_count=len(skipped),
            failed_count=len(failed),
            items=items,
            skipped=skipped,
            failed=failed,
        )

    async def ingest_policy_reference_documents_with_openai_vision(
        self,
        limit: int = 10,
    ) -> PolicyReferenceDocumentIngestResponse:
        items: list[PolicyReferenceDocumentIngestItem] = []
        skipped: list[PolicyReferenceDocumentSkipItem] = []
        failed: list[dict[str, str]] = []

        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY가 설정되어 있지 않습니다.")

        async with psycopg_pool.connection() as conn:
            targets = await PolicyDocumentRepository.find_policy_reference_download_targets(
                conn=conn,
                limit=limit,
            )
            targets_by_url = self._group_by_source_url(targets)

            for source_url, documents in targets_by_url.items():
                try:
                    downloaded = self._download_reference_document(source_url)
                    file_type = self._detect_file_type(
                        content=downloaded["content"],
                        source_title=documents[0]["source_title"],
                        content_type=downloaded.get("content_type"),
                    )
                    if file_type != "PDF":
                        for document in documents:
                            skipped.append(
                                self._reference_skip_item(
                                    document=document,
                                    reason=(
                                        "OpenAI Vision 보완 추출은 PDF 문서만 "
                                        f"지원합니다: {file_type}"
                                    ),
                                )
                            )
                        continue

                    content = downloaded["content"]
                    if not self._is_pdf_content(content):
                        for document in documents:
                            skipped.append(
                                self._reference_skip_item(
                                    document=document,
                                    reason=(
                                        "문서명이 PDF이지만 실제 PDF 바이트가 "
                                        "아니어서 처리하지 않았습니다."
                                    ),
                                )
                            )
                        continue

                    if len(content) > POLICY_REFERENCE_OPENAI_MAX_BYTES:
                        for document in documents:
                            skipped.append(
                                self._reference_skip_item(
                                    document=document,
                                    reason=(
                                        "OpenAI 파일 입력 제한을 초과해 처리하지 "
                                        f"않았습니다: {len(content)} bytes"
                                    ),
                                )
                            )
                        continue

                    raw_text = await self._extract_pdf_text_with_openai_vision(
                        content=content,
                        filename=self._safe_pdf_filename(documents[0]["source_title"]),
                    )
                    if not raw_text:
                        for document in documents:
                            skipped.append(
                                self._reference_skip_item(
                                    document=document,
                                    reason="OpenAI Vision으로 추출된 텍스트가 없습니다.",
                                )
                            )
                        continue

                    for document in documents:
                        chunk_documents = self.split_reference_documents(
                            self._build_policy_reference_documents(
                                source=document,
                                raw_text=raw_text,
                                file_type="PDF",
                                extraction_result=PdfExtractionResult(
                                    method="openai_vision",
                                    text=raw_text,
                                    quality_score=self._text_quality_score(raw_text)[0],
                                    metrics=self._text_quality_score(raw_text)[1],
                                ),
                            )
                        )
                        async with conn.transaction():
                            await PolicyDocumentRepository.update_document_raw_text(
                                conn=conn,
                                document_id=document["document_id"],
                                raw_text=raw_text,
                            )
                            chunk_count = (
                                await PolicyDocumentRepository.replace_document_chunks(
                                    conn=conn,
                                    document_id=document["document_id"],
                                    chunk_documents=chunk_documents,
                                )
                            )

                        items.append(
                            PolicyReferenceDocumentIngestItem(
                                document_id=document["document_id"],
                                policy_id=document["policy_id"],
                                policy_code=document["policy_code"],
                                policy_name=document["policy_name"],
                                source_title=document["source_title"],
                                source_url=document["source_url"],
                                file_type="PDF",
                                raw_text_length=len(raw_text),
                                chunk_count=chunk_count,
                            )
                        )
                except ValueError as exc:
                    for document in documents:
                        skipped.append(
                            self._reference_skip_item(
                                document=document,
                                reason=str(exc),
                            )
                        )
                except Exception as exc:
                    self.logger.exception(
                        "OpenAI Vision 기반 정책 관련 문서 처리 중 오류 발생"
                    )
                    failed.append(
                        {
                            "source_url": source_url,
                            "document_ids": ",".join(
                                str(document["document_id"]) for document in documents
                            ),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

        return PolicyReferenceDocumentIngestResponse(
            requested_url_count=len(targets_by_url),
            completed_count=len(items),
            skipped_count=len(skipped),
            failed_count=len(failed),
            items=items,
            skipped=skipped,
            failed=failed,
        )

    def split_documents(self, documents: list[Document]) -> list[Document]:
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=50,
            length_function=len,
        )
        return text_splitter.split_documents(documents)

    def split_policy_detail_documents(self, documents: list[Document]) -> list[Document]:
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1200,
            chunk_overlap=100,
            length_function=len,
        )

        split_documents: list[Document] = []
        for document in documents:
            if document.metadata.get("preserve_chunk"):
                metadata = dict(document.metadata)
                metadata.pop("preserve_chunk", None)
                split_documents.append(
                    Document(
                        page_content=document.page_content,
                        metadata=metadata,
                    )
                )
                continue

            for split_document in text_splitter.split_documents([document]):
                metadata = dict(split_document.metadata)
                metadata.pop("preserve_chunk", None)
                split_documents.append(
                    Document(
                        page_content=split_document.page_content,
                        metadata=metadata,
                    )
                )
        return split_documents

    def split_reference_documents(self, documents: list[Document]) -> list[Document]:
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1800,
            chunk_overlap=200,
            length_function=len,
        )
        return text_splitter.split_documents(documents)

    def _group_by_source_url(
        self,
        targets: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for target in targets:
            grouped[target["source_url"]].append(target)
        return dict(grouped)

    def _download_reference_document(self, source_url: str) -> dict[str, Any]:
        response = requests.get(source_url, timeout=60)
        response.raise_for_status()
        return {
            "content": response.content,
            "content_type": response.headers.get("content-type"),
        }

    def _detect_file_type(
        self,
        content: bytes,
        source_title: str,
        content_type: str | None,
    ) -> str:
        lowered_title = source_title.lower()
        lowered_content_type = (content_type or "").lower()
        if content.startswith(b"%PDF") or ".pdf" in lowered_title:
            return "PDF"
        if (
            content.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
            or ".hwp" in lowered_title
        ):
            return "HWP"
        if content.startswith(b"PK\x03\x04") and ".hwpx" in lowered_title:
            return "HWPX"
        if "html" in lowered_content_type:
            return "HTML"
        return "UNKNOWN"

    def _extract_text(self, content: bytes, file_type: str) -> PdfExtractionResult:
        if file_type == "PDF":
            return self._extract_pdf_text(content)
        if file_type == "HWP":
            return self._extract_hwp_text(content)
        if file_type == "HWPX":
            raise ValueError("HWPX 문서 텍스트 추출은 아직 지원하지 않습니다.")
        raise ValueError(f"지원하지 않는 문서 형식입니다: {file_type}")

    def _extract_hwp_text(self, content: bytes) -> PdfExtractionResult:
        """Extract HWP v5 text through pyhwp's bundled hwp5txt command."""
        converter = (Path(sys.executable).parent / "hwp5txt")
        if not converter.exists():
            raise ValueError("HWP 추출기(hwp5txt)가 설치되어 있지 않습니다.")

        with tempfile.NamedTemporaryFile(suffix=".hwp") as source:
            source.write(content)
            source.flush()
            try:
                result = subprocess.run(
                    [str(converter), source.name],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"HWP 텍스트 추출 실패: {exc}") from exc

        return self._pdf_extraction_result(method="hwp5txt", text=result.stdout)

    def _extract_pdf_text(self, content: bytes) -> PdfExtractionResult:
        candidates = []
        primary_result = self._extract_pdf_text_with_pypdfium2(content)
        candidates.append(primary_result)
        if self._is_good_pdf_extraction(primary_result):
            return primary_result

        pymupdf_result = self._extract_pdf_text_with_pymupdf(content)
        candidates.append(pymupdf_result)
        if self._is_good_pdf_extraction(pymupdf_result):
            pymupdf_result.metrics["fallback_from"] = primary_result.method
            return pymupdf_result

        if primary_result.text or pymupdf_result.text:
            pdfplumber_result = self._extract_pdf_text_with_pdfplumber(content)
            candidates.append(pdfplumber_result)
            if self._is_good_pdf_extraction(pdfplumber_result):
                pdfplumber_result.metrics["fallback_from"] = primary_result.method
                return pdfplumber_result

        valid_candidates = [candidate for candidate in candidates if candidate.text]
        if not valid_candidates:
            return PdfExtractionResult(
                method="none",
                text="",
                quality_score=0,
                metrics={
                    "error": "추출된 텍스트가 없습니다.",
                    "ocr_recommended": True,
                },
            )
        best_candidate = max(valid_candidates, key=lambda candidate: candidate.quality_score)
        if not self._is_good_pdf_extraction(best_candidate):
            best_candidate.metrics["ocr_recommended"] = True
        return best_candidate

    def _extract_pdf_text_with_pypdfium2(self, content: bytes) -> PdfExtractionResult:
        try:
            pdf = pdfium.PdfDocument(BytesIO(content))
            page_texts = []
            rotated_page_count = 0
            reversed_page_count = 0
            for page in pdf:
                rotation = int(page.get_rotation() or 0)
                if rotation:
                    rotated_page_count += 1
                textpage = page.get_textpage()
                page_text, was_reversed = self._normalize_pdf_page_text(
                    textpage.get_text_range() or "",
                    rotation=rotation,
                )
                if was_reversed:
                    reversed_page_count += 1
                page_texts.append(page_text)
            result = self._pdf_extraction_result(
                method="pypdfium2",
                text="\n\n".join(page_text for page_text in page_texts if page_text),
            )
            result.metrics["rotated_page_count"] = rotated_page_count
            result.metrics["reversed_page_count"] = reversed_page_count
            return result
        except Exception as exc:
            return PdfExtractionResult(
                method="pypdfium2",
                text="",
                quality_score=0,
                metrics={"error": f"{type(exc).__name__}: {exc}"},
            )

    def _extract_pdf_text_with_pymupdf(self, content: bytes) -> PdfExtractionResult:
        if fitz is None:
            return PdfExtractionResult(
                method="pymupdf",
                text="",
                quality_score=0,
                metrics={"error": "PyMuPDF가 설치되어 있지 않습니다."},
            )
        try:
            with fitz.open(stream=content, filetype="pdf") as pdf:
                page_texts = [
                    self._clean_extracted_text(page.get_text("text") or "")
                    for page in pdf
                ]
            return self._pdf_extraction_result(
                method="pymupdf",
                text="\n\n".join(page_text for page_text in page_texts if page_text),
            )
        except Exception as exc:
            return PdfExtractionResult(
                method="pymupdf",
                text="",
                quality_score=0,
                metrics={"error": f"{type(exc).__name__}: {exc}"},
            )

    def _extract_pdf_text_with_pdfplumber(self, content: bytes) -> PdfExtractionResult:
        try:
            with pdfplumber.open(BytesIO(content)) as pdf:
                page_texts = [
                    self._clean_extracted_text(page.extract_text() or "")
                    for page in pdf.pages
                ]
            return self._pdf_extraction_result(
                method="pdfplumber",
                text="\n\n".join(page_text for page_text in page_texts if page_text),
            )
        except Exception as exc:
            return PdfExtractionResult(
                method="pdfplumber",
                text="",
                quality_score=0,
                metrics={"error": f"{type(exc).__name__}: {exc}"},
            )

    def _normalize_pdf_page_text(
        self,
        text: str,
        rotation: int = 0,
    ) -> tuple[str, bool]:
        cleaned_text = self._clean_extracted_text(text)
        if not cleaned_text:
            return "", False

        lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]
        if not lines or not self._looks_like_reversed_pdf_page(cleaned_text, rotation):
            return cleaned_text, False

        reversed_lines = "\n".join(line[::-1] for line in lines)
        reversed_lines_and_order = "\n".join(line[::-1] for line in reversed(lines))
        candidates = [
            (cleaned_text, self._korean_text_order_score(cleaned_text)),
            (reversed_lines, self._korean_text_order_score(reversed_lines)),
            (
                reversed_lines_and_order,
                self._korean_text_order_score(reversed_lines_and_order),
            ),
        ]
        best_text, best_score = max(candidates, key=lambda candidate: candidate[1])
        original_score = candidates[0][1]
        if best_text != cleaned_text and best_score >= original_score + 3:
            return best_text, True
        return cleaned_text, False

    def _looks_like_reversed_pdf_page(self, text: str, rotation: int = 0) -> bool:
        hangul_count = len(re.findall(r"[가-힣]", text))
        if hangul_count < 30:
            return False
        if rotation in {90, 180, 270}:
            return True
        return self._korean_text_order_score(text) <= -3

    def _korean_text_order_score(self, text: str) -> int:
        normal_tokens = (
            "지원",
            "신청",
            "대상",
            "기준",
            "서비스",
            "사업",
            "내용",
            "방법",
            "절차",
            "서류",
            "제출",
            "대상자",
            "가구",
            "소득",
            "장애",
            "아동",
            "임산부",
            "가능",
            "필요",
            "합니다",
            "습니다",
            "입니다",
            "됩니다",
            "있습니다",
            "바랍니다",
        )
        reversed_tokens = tuple(token[::-1] for token in normal_tokens)
        return sum(text.count(token) for token in normal_tokens) - sum(
            text.count(token) for token in reversed_tokens
        )

    def _is_good_pdf_extraction(self, result: PdfExtractionResult) -> bool:
        if not result.text:
            return False
        metrics = result.metrics
        return (
            metrics.get("text_length", 0) >= 1000
            and metrics.get("replacement_count", 0) <= 5
            and metrics.get("space_ratio", 1) <= 0.4
            and metrics.get("spaced_hangul_runs", 0) <= 800
        )

    def _pdf_extraction_result(
        self,
        method: str,
        text: str,
    ) -> PdfExtractionResult:
        quality_score, metrics = self._text_quality_score(text)
        return PdfExtractionResult(
            method=method,
            text=text,
            quality_score=quality_score,
            metrics=metrics,
        )

    def _text_quality_score(self, text: str) -> tuple[float, dict[str, Any]]:
        cleaned_text = self._clean_extracted_text(text)
        compact_text = re.sub(r"\s+", "", cleaned_text)
        replacement_count = cleaned_text.count("\ufffd") + cleaned_text.count("�")
        spaced_hangul_runs = len(
            re.findall(r"(?:[가-힣]\s+){4,}[가-힣]", cleaned_text)
        )
        spaced_latin_runs = len(
            re.findall(r"(?:[A-Za-z]\s+){4,}[A-Za-z]", cleaned_text)
        )
        space_ratio = (
            (len(cleaned_text) - len(compact_text)) / max(len(compact_text), 1)
        )
        header_count = sum(
            1
            for line in cleaned_text.splitlines()
            if self._is_reference_header(line)
        )
        length_score = min(len(cleaned_text) / 2000, 1.0) * 30
        header_score = min(header_count, 30) * 1.0
        penalty = (
            replacement_count * 2
            + spaced_hangul_runs * 0.2
            + spaced_latin_runs * 1
            + max(space_ratio - 0.35, 0) * 180
        )
        if len(cleaned_text.strip()) < 200:
            penalty += 100

        score = max(0.0, 100 + length_score + header_score - penalty)
        return (
            round(score, 3),
            {
                "text_length": len(cleaned_text),
                "replacement_count": replacement_count,
                "spaced_hangul_runs": spaced_hangul_runs,
                "spaced_latin_runs": spaced_latin_runs,
                "space_ratio": round(space_ratio, 3),
                "header_count": header_count,
            },
        )

    def _is_pdf_content(self, content: bytes) -> bool:
        return content.startswith(b"%PDF")

    async def _extract_pdf_text_with_openai_vision(
        self,
        content: bytes,
        filename: str,
    ) -> str:
        encoded_pdf = base64.b64encode(content).decode("ascii")
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.responses.create(
            model=POLICY_REFERENCE_VISION_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "filename": filename,
                            "file_data": (
                                "data:application/pdf;base64,"
                                f"{encoded_pdf}"
                            ),
                        },
                        {
                            "type": "input_text",
                            "text": (
                                "이 PDF에서 보이는 텍스트만 추출하세요. "
                                "가능한 한 원문 표현을 유지하세요. "
                                "표는 각 행을 일반 텍스트로 풀어서 작성하세요. "
                                "요약하거나 설명하지 마세요."
                            ),
                        },
                    ],
                }
            ],
        )
        return self._clean_text(response.output_text)

    def _safe_pdf_filename(self, source_title: str) -> str:
        filename = self._clean_text(source_title) or "policy-reference.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        return filename

    def _build_policy_reference_documents(
        self,
        source: dict[str, Any],
        raw_text: str,
        file_type: str,
        extraction_result: PdfExtractionResult,
    ) -> list[Document]:
        sections = self._reference_sections(raw_text)
        return [
            Document(
                page_content=self._format_section_text(
                    policy_name=source["policy_name"],
                    section=section["section"],
                    content=section["content"],
                ),
                metadata={
                    "policy_id": source["policy_id"],
                    "policy_code": source["policy_code"],
                    "source_type": "POLICY_REFERENCE",
                    "source_title": source["source_title"],
                    "source_url": source["source_url"],
                    "section": section["section"],
                    "reference_section_index": section["section_index"],
                    "evidence_role": "reference",
                    "file_type": file_type,
                    "extraction_method": extraction_result.method,
                    "extraction_quality_score": extraction_result.quality_score,
                    "extraction_metrics": extraction_result.metrics,
                },
            )
            for section in sections
        ]

    def _reference_sections(self, raw_text: str) -> list[dict[str, Any]]:
        cleaned_text = self._clean_extracted_text(raw_text)
        sections: list[dict[str, Any]] = []
        current_title = "관련 문서"
        current_lines: list[str] = []

        for line in cleaned_text.splitlines():
            stripped_line = line.strip()
            if not stripped_line:
                continue
            if self._is_reference_header(stripped_line):
                if current_lines:
                    sections.append(
                        {
                            "section": current_title,
                            "content": "\n".join(current_lines).strip(),
                        }
                    )
                current_title = stripped_line[:120]
                current_lines = [stripped_line]
                continue
            current_lines.append(stripped_line)

        if current_lines:
            sections.append(
                {
                    "section": current_title,
                    "content": "\n".join(current_lines).strip(),
                }
            )

        if not sections:
            sections = [{"section": "관련 문서", "content": cleaned_text}]

        packed_sections = self._pack_reference_sections(sections)
        return [
            {
                "section_index": index,
                "section": section["section"],
                "content": section["content"],
            }
            for index, section in enumerate(packed_sections)
            if section["content"]
        ]

    def _pack_reference_sections(
        self,
        sections: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        packed: list[dict[str, str]] = []
        current_title: str | None = None
        current_parts: list[str] = []
        current_length = 0

        for section in sections:
            title = section["section"]
            content = section["content"]
            next_part = content
            next_length = len(next_part)
            should_flush = (
                current_parts
                and current_length + next_length > POLICY_REFERENCE_SECTION_TARGET_CHARS
            )
            if should_flush:
                packed.append(
                    {
                        "section": current_title or "관련 문서",
                        "content": "\n\n".join(current_parts).strip(),
                    }
                )
                current_title = None
                current_parts = []
                current_length = 0

            if current_title is None:
                current_title = title
            current_parts.append(next_part)
            current_length += next_length

        if current_parts:
            packed.append(
                {
                    "section": current_title or "관련 문서",
                    "content": "\n\n".join(current_parts).strip(),
                }
            )

        return packed

    def _is_reference_header(self, line: str) -> bool:
        cleaned_line = line.strip()
        if not cleaned_line or len(cleaned_line) > 120:
            return False
        if cleaned_line.count("·") >= 5:
            return False
        if not REFERENCE_HEADER_PATTERN.match(cleaned_line):
            return False
        if re.match(r"^\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.\s]+", cleaned_line):
            return True
        if re.match(r"^\s*제\s*\d+\s*[장절]\s+", cleaned_line):
            return True
        return any(keyword in cleaned_line for keyword in REFERENCE_HEADER_KEYWORDS)

    def _clean_extracted_text(self, value: Any) -> str:
        text = self._clean_text(value)
        text = text.replace("\ufeff", "").replace("\u00a0", " ")
        text = text.replace("\x0c", "\n")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]{3,}", "  ", text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip()

    def _reference_skip_item(
        self,
        document: dict[str, Any],
        reason: str,
    ) -> PolicyReferenceDocumentSkipItem:
        return PolicyReferenceDocumentSkipItem(
            document_id=document["document_id"],
            policy_id=document["policy_id"],
            policy_code=document["policy_code"],
            policy_name=document["policy_name"],
            source_title=document["source_title"],
            source_url=document.get("source_url"),
            reason=reason,
        )

    def _build_policy_detail_documents(
        self,
        source: dict[str, Any],
    ) -> tuple[str, list[Document]]:
        section_documents: list[Document] = []
        for section_value in self._section_values(source):
            cleaned_content = self._clean_text(section_value["content"])
            if not cleaned_content:
                continue

            metadata = self._policy_detail_metadata(source, section_value)
            section_documents.append(
                Document(
                    page_content=self._format_section_text(
                        policy_name=source["policy_name"],
                        section=section_value["section"],
                        content=cleaned_content,
                    ),
                    metadata=metadata,
                )
            )

        raw_text = "\n\n".join(document.page_content for document in section_documents)
        return raw_text, section_documents

    def _section_values(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        condition_json = self._dict_value(source.get("condition_json"))
        sections: list[dict[str, Any]] = [
            {
                "section": "정리된 지원 조건",
                "evidence_role": "target",
                "content": source.get("target_summary")
                or condition_json.get("target_summary"),
                "condition_group": "target_summary",
                "condition_operator": None,
                "preserve_chunk": True,
            },
            {
                "section": "조건 구조",
                "evidence_role": "target",
                "content": self._format_condition_tree(
                    condition_json.get("condition_tree")
                ),
                "condition_group": "condition_tree",
                "condition_operator": self._node_operator(
                    condition_json.get("condition_tree")
                ),
                "preserve_chunk": True,
            },
            {
                "section": "제외 조건",
                "evidence_role": "caution",
                "content": self._format_condition_items(
                    condition_json.get("exclusions")
                ),
                "condition_group": "exclusions",
                "condition_operator": "NOT",
                "preserve_chunk": True,
            },
            {
                "section": "추가 확인 조건",
                "evidence_role": "caution",
                "content": self._format_condition_items(
                    self._list_value(condition_json.get("unknowns"))
                    + self._list_value(
                        condition_json.get("unsupported_conditions")
                    )
                    + self._list_value(condition_json.get("special_notes"))
                ),
                "condition_group": "manual_check",
                "condition_operator": None,
                "preserve_chunk": True,
            },
            {
                "section": "공식 지원대상 원문",
                "evidence_role": "target",
                "content": source.get("source_text")
                or source.get("target_description"),
                "condition_group": "official_target_text",
                "condition_operator": None,
                "source_basis": "policy_detail",
            },
            {
                "section": "지원 내용",
                "evidence_role": "benefit",
                "content": source.get("benefit_description"),
                "condition_group": None,
                "condition_operator": None,
                "source_basis": "policy_detail",
            },
            {
                "section": "신청 방법",
                "evidence_role": "application",
                "content": source.get("application_method"),
                "condition_group": None,
                "condition_operator": None,
                "source_basis": "policy_detail",
            },
            {
                "section": "신청 기간",
                "evidence_role": "application",
                "content": source.get("application_period_text"),
                "condition_group": None,
                "condition_operator": None,
                "source_basis": "policy_detail",
            },
            {
                "section": "유의 사항",
                "evidence_role": "caution",
                "content": source.get("caution"),
                "condition_group": None,
                "condition_operator": None,
                "source_basis": "policy_detail",
            },
        ]
        return sections

    def _policy_detail_metadata(
        self,
        source: dict[str, Any],
        section_value: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = {
            "policy_id": source["policy_id"],
            "policy_code": source["policy_code"],
            "policy_name": source["policy_name"],
            "condition_profile_id": source.get("condition_profile_id"),
            "condition_profile_updated_at": self._to_metadata_value(
                source.get("condition_profile_updated_at")
            ),
            "condition_profile_confidence": self._to_metadata_value(
                source.get("confidence")
            ),
            "review_required": source.get("review_required"),
            "quality_flags": source.get("quality_flags"),
            "source_fields": source.get("source_fields"),
            "source_type": "POLICY_DETAIL",
            "source_basis": section_value.get(
                "source_basis",
                "policy_condition_profile",
            ),
            "source_title": self._source_title(source),
            "source_url": source.get("official_url"),
            "section": section_value["section"],
            "evidence_role": section_value["evidence_role"],
            "condition_group": section_value.get("condition_group"),
            "condition_operator": section_value.get("condition_operator"),
            "preserve_chunk": section_value.get("preserve_chunk", False),
        }
        return {key: value for key, value in metadata.items() if value is not None}

    def _format_condition_tree(self, node: Any, depth: int = 0) -> str:
        if not node:
            return ""
        if not isinstance(node, dict):
            return self._clean_text(node)

        indent = "  " * depth
        children = self._node_children(node)
        operator = self._node_operator(node)
        if children:
            header = f"{indent}- 조건 그룹: {operator or 'AND'}"
            lines = [header]
            for child in children:
                child_text = self._format_condition_tree(child, depth + 1)
                if child_text:
                    lines.append(child_text)
            return "\n".join(lines)

        return f"{indent}- {self._format_condition_leaf(node)}"

    def _format_condition_items(self, items: Any) -> str:
        if not items:
            return ""
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return self._clean_text(items)

        lines = []
        for item in items:
            if isinstance(item, dict):
                lines.append(f"- {self._format_condition_leaf(item)}")
            else:
                cleaned = self._clean_text(item)
                if cleaned:
                    lines.append(f"- {cleaned}")
        return "\n".join(lines)

    def _format_condition_leaf(self, node: dict[str, Any]) -> str:
        label_values = [
            ("field", node.get("field") or node.get("field_name")),
            ("operator", node.get("operator")),
            (
                "value",
                node.get("value")
                if "value" in node
                else node.get("value_json"),
            ),
            ("matching_strength", node.get("matching_strength")),
            ("source_text", node.get("source_text")),
            ("confidence", node.get("confidence")),
            ("note", node.get("note")),
            ("reason", node.get("reason")),
        ]
        return self._join_lines(label_values).replace("\n", ", ")

    def _node_children(self, node: dict[str, Any]) -> list[Any]:
        for key in ("children", "conditions", "items", "rules"):
            value = node.get(key)
            if isinstance(value, list):
                return value
        return []

    def _node_operator(self, node: Any) -> str | None:
        if not isinstance(node, dict):
            return None
        operator = (
            node.get("operator")
            or node.get("condition_operator")
            or node.get("group_operator")
            or node.get("logic")
            or node.get("type")
        )
        if operator is None:
            return None
        return str(operator).upper()

    def _dict_value(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _list_value(self, value: Any) -> list[Any]:
        if not value:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def _to_metadata_value(self, value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if isinstance(value, (str, int, float, bool, list, dict)):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)

    def _join_lines(self, values: list[tuple[str, Any]]) -> str:
        lines = [
            f"{label}: {self._clean_text(value)}"
            for label, value in values
            if self._clean_text(value)
        ]
        return "\n".join(lines)

    def _format_section_text(
        self,
        policy_name: str,
        section: str,
        content: str,
    ) -> str:
        return f"정책명: {policy_name}\n섹션: {section}\n내용:\n{content}"

    def _source_title(self, source: dict[str, Any]) -> str:
        return f"{source['policy_name']} 정책 상세 데이터"

    def _clean_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value).replace("\x00", "").strip()


PolicyDocumentServiceDep = Annotated[
    PolicyDocumentService,
    Depends(PolicyDocumentService),
]
