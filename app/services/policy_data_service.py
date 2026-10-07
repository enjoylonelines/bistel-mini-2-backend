import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import unquote

import requests
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.repositories.policy_raw_import_repository import PolicyRawImportRepository


class PolicyDataService:
    endpoint = "https://apis.data.go.kr/B554287/NationalWelfareInformationsV001"

    @classmethod
    def get_welfare_list_xml(
        cls,
        call_tp: str,
        page_no: int,
        num_of_rows: int,
        srch_key_code: str,
        search_wrd: str | None = None,
        life_array: str | None = None,
        trgter_indvdl_array: str | None = None,
        intrs_thema_array: str | None = None,
        age: str | None = None,
        onap_psblt_yn: str | None = None,
        order_by: str | None = None,
    ) -> str:
        params = {
            "serviceKey": cls._get_service_key(),
            "callTp": call_tp,
            "pageNo": page_no,
            "numOfRows": num_of_rows,
            "srchKeyCode": srch_key_code,
            "searchWrd": search_wrd,
            "lifeArray": life_array,
            "trgterIndvdlArray": trgter_indvdl_array,
            "intrsThemaArray": intrs_thema_array,
            "age": age,
            "onapPsbltYn": onap_psblt_yn,
            "orderBy": order_by,
        }
        return cls._request_xml("/NationalWelfarelistV001", params)

    @classmethod
    def get_welfare_detail_xml(cls, call_tp: str, serv_id: str) -> str:
        params = {
            "serviceKey": cls._get_service_key(),
            "callTp": call_tp,
            "servId": serv_id,
        }
        return cls._request_xml("/NationalWelfaredetailedV001", params)

    @staticmethod
    async def create_raw_import_table(db: AsyncSession) -> None:
        await PolicyRawImportRepository.create_table(db)

    @classmethod
    async def save_list_xml_to_db(
        cls,
        db: AsyncSession,
        xml_text: str,
    ) -> dict[str, int]:
        items = cls._find_xml_items(xml_text, "servList")
        saved_count = 0
        skipped_count = 0

        for item in items:
            serv_id = item.get("servId")
            if not serv_id:
                skipped_count += 1
                continue

            await PolicyRawImportRepository.upsert_list_item(db, serv_id, item)
            saved_count += 1

        await PolicyRawImportRepository.record_ingestion_attempt(
            db,
            source_locator=f"{cls.endpoint}/NationalWelfarelistV001",
            stage="RAW_LIST_SAVE",
            payload=xml_text,
            counters={"saved_count": saved_count, "skipped_count": skipped_count},
        )

        return {
            "saved_count": saved_count,
            "skipped_count": skipped_count,
        }

    @classmethod
    async def save_detail_xml_to_db(
        cls,
        db: AsyncSession,
        serv_id: str,
        xml_text: str,
    ) -> dict[str, str]:
        detail_json = cls._xml_to_dict(ET.fromstring(xml_text.strip()))
        detail_json["applmetList"] = cls._find_xml_items(xml_text, "applmetList")
        detail_json["inqplCtadrList"] = cls._find_xml_items(xml_text, "inqplCtadrList")
        detail_json["inqplHmpgReldList"] = cls._find_xml_items(
            xml_text,
            "inqplHmpgReldList",
        )
        detail_json["basfrmList"] = cls._find_xml_items(xml_text, "basfrmList")
        detail_json["baslawList"] = cls._find_xml_items(xml_text, "baslawList")

        await PolicyRawImportRepository.upsert_detail_item(
            db,
            serv_id,
            detail_json,
        )
        await PolicyRawImportRepository.record_ingestion_attempt(
            db,
            source_locator=(
                f"{cls.endpoint}/NationalWelfaredetailedV001?servId={serv_id}"
            ),
            stage="RAW_DETAIL_SAVE",
            payload=xml_text,
            counters={"saved_count": 1, "skipped_count": 0},
        )

        return {
            "serv_id": serv_id,
            "detail_status": "COMPLETED",
        }

    @staticmethod
    async def get_pending_serv_ids(db: AsyncSession, limit: int = 10) -> list[str]:
        return await PolicyRawImportRepository.find_pending_serv_ids(db, limit)

    @staticmethod
    async def save_detail_failed(
        db: AsyncSession,
        serv_id: str,
        error_message: str,
    ) -> None:
        await PolicyRawImportRepository.mark_detail_failed(
            db,
            serv_id,
            error_message,
        )

    @classmethod
    def _request_xml(cls, path: str, params: dict[str, Any]) -> str:
        filtered_params = {
            key: value
            for key, value in params.items()
            if value not in (None, "")
        }
        response = requests.get(
            f"{cls.endpoint}{path}",
            params=filtered_params,
            timeout=30,
        )
        response.raise_for_status()
        return response.text

    @classmethod
    def _find_xml_items(cls, xml_text: str, tag_name: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text.strip())
        return [
            cls._xml_to_dict(node)
            for node in root.iter()
            if cls._strip_namespace(node.tag) == tag_name
        ]

    @classmethod
    def _xml_to_dict(cls, node: ET.Element) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for child in list(node):
            key = cls._strip_namespace(child.tag)
            if list(child):
                value = cls._xml_to_dict(child)
            else:
                value = (child.text or "").strip()

            if key in result:
                if not isinstance(result[key], list):
                    result[key] = [result[key]]
                result[key].append(value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _strip_namespace(tag: str) -> str:
        return tag.split("}", 1)[-1]

    @staticmethod
    def _get_service_key() -> str:
        if not settings.data_go_kr_service_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="DATA_GO_KR_SERVICE_KEY is not configured.",
            )
        # data.go.kr exposes both URL-encoded and decoded variants of the
        # general authentication key. ``requests`` encodes query params, so
        # normalize the configured value to its decoded form first to avoid
        # double-encoding an otherwise valid key.
        return unquote(settings.data_go_kr_service_key)
