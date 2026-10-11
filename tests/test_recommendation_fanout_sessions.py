import asyncio
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai.nodes.recommendation.recommendation_nodes import RecommendationGraphNodes
from app.services.recommendation_candidate_service import (
    CANDIDATE_STATUS_CANDIDATE,
    PolicyCandidate,
)
from app.services.recommendation_evidence_lane import ProcessLocalEvidenceLane
from app.services.recommendation_service import RecommendationService


def _candidate(policy_id: int) -> PolicyCandidate:
    return PolicyCandidate(
        policy=SimpleNamespace(
            policy_id=policy_id,
            policy_name=f"정책 {policy_id}",
            policy_code=f"P{policy_id}",
            benefit_type="지원",
        ),
        detail=SimpleNamespace(
            target_description="대상", benefit_description="혜택",
            application_method=None, application_period_text=None, caution=None,
            easy_summary=None,
        ),
        retrieval_score=0.8,
        candidate_status=CANDIDATE_STATUS_CANDIDATE,
        filter_match_json={"reasons": {"matched": [], "uncertain": [], "excluded": []}},
        matched_rules=[],
    )


class _NoopRerankService:
    def select_candidate_pool(self, *, candidates, **_kwargs):
        return candidates


class _NoopEventRepository:
    async def record(self, *_args, **_kwargs):
        return None


def test_overlapping_fanout_uses_distinct_actual_async_sessions(tmp_path: Path) -> None:
    async def exercise() -> tuple[list[int], int, list[int], int]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fanout.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        entered = asyncio.Event()
        release_checks = asyncio.Event()
        checked_session_ids: list[int] = []
        active_checks = 0
        max_active_checks = 0
        search_calls: list[int] = []

        class Repository:
            async def has_active_recommendation_execution(self, db, *_args):
                nonlocal active_checks, max_active_checks
                await db.execute(text("SELECT 1"))
                checked_session_ids.append(id(db))
                active_checks += 1
                max_active_checks = max(max_active_checks, active_checks)
                if len(checked_session_ids) == 2:
                    entered.set()
                await release_checks.wait()
                active_checks -= 1
                return True

        async def chunk_searcher(*, policy_ids, **_kwargs):
            search_calls.append(policy_ids[0])
            return []

        nodes = RecommendationGraphNodes(
            recommendation_service=RecommendationService(chunk_searcher=chunk_searcher),
            rerank_service=_NoopRerankService(),
            request_repository=Repository(),
            execution_event_repository=_NoopEventRepository(),
            execution_session_factory=sessions,
        )
        async with sessions() as parent_db:
            task = asyncio.create_task(
                nodes.build_result(
                    {
                        "db": parent_db,
                        "request_id": 91,
                        "execution_token": "owner",
                        "merged_condition_json": {},
                        "candidates": [_candidate(1), _candidate(2)],
                        "assessments": [],
                    }
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            release_checks.set()
            await task
            parent_id = id(parent_db)
        await engine.dispose()
        return checked_session_ids, parent_id, search_calls, max_active_checks

    checked_session_ids, parent_id, search_calls, max_active_checks = asyncio.run(exercise())

    assert len(checked_session_ids) == 2
    assert len(set(checked_session_ids)) == 2
    assert parent_id not in checked_session_ids
    assert max_active_checks == 2
    assert sorted(search_calls) == [1, 2]


def test_cancelled_waiting_fanout_search_never_starts(tmp_path: Path) -> None:
    async def exercise() -> tuple[list[int], list[int]]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancelled.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        first_search_started = asyncio.Event()
        release_first_search = asyncio.Event()
        active = True
        checked_session_ids: list[int] = []
        search_calls: list[int] = []

        class Repository:
            async def has_active_recommendation_execution(self, db, *_args):
                await db.execute(text("SELECT 1"))
                checked_session_ids.append(id(db))
                return active

        async def chunk_searcher(*, policy_ids, **_kwargs):
            policy_id = policy_ids[0]
            search_calls.append(policy_id)
            if policy_id == 1:
                first_search_started.set()
                await release_first_search.wait()
            return []

        nodes = RecommendationGraphNodes(
            recommendation_service=RecommendationService(
                chunk_searcher=chunk_searcher,
                evidence_lane=ProcessLocalEvidenceLane(capacity=1),
            ),
            rerank_service=_NoopRerankService(),
            request_repository=Repository(),
            execution_event_repository=_NoopEventRepository(),
            execution_session_factory=sessions,
        )
        async with sessions() as parent_db:
            task = asyncio.create_task(
                nodes.build_result(
                    {
                        "db": parent_db,
                        "request_id": 92,
                        "execution_token": "owner",
                        "merged_condition_json": {},
                        "candidates": [_candidate(1), _candidate(2)],
                        "assessments": [],
                    }
                )
            )
            await asyncio.wait_for(first_search_started.wait(), timeout=1)
            active = False
            release_first_search.set()
            result = await task
            parent_id = id(parent_db)
        await engine.dispose()
        assert result["result_json"]["summary"]["evidence_search_avoided_call_count"] == 1
        assert parent_id not in checked_session_ids
        return checked_session_ids, search_calls

    checked_session_ids, search_calls = asyncio.run(exercise())

    assert len(checked_session_ids) == 2
    assert search_calls == [1]
