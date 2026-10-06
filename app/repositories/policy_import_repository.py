class PolicyImportRepository:
    # ============================================================
    # 공통 실행 메소드
    # ============================================================
    @staticmethod
    async def _fetch_count(conn, query: str) -> int:
        async with conn.cursor() as cur:
            await cur.execute(query)
            row = await cur.fetchone()
            return int(row[0]) if row is not None else 0

    @staticmethod
    async def _execute(conn, query: str) -> None:
        async with conn.cursor() as cur:
            await cur.execute(query)

    # ============================================================
    # 1. policy / policy_detail 저장
    # ============================================================
    @classmethod
    async def upsert_policies(cls, conn) -> int:
        return await cls._fetch_count(
            conn,
            """
                WITH raw AS (
                    SELECT
                        import_id,
                        serv_id,
                        list_json,
                        detail_json,
                        detail_fetched_at
                    FROM policy_raw_import
                    WHERE list_json IS NOT NULL
                      AND detail_json IS NOT NULL
                      AND detail_status = 'COMPLETED'
                ),
                upserted AS (
                    INSERT INTO policy (
                        policy_id,
                        policy_code,
                        policy_name,
                        main_category,
                        sub_category,
                        provider_name,
                        provider_type,
                        region_scope,
                        region_code,
                        benefit_type,
                        application_status,
                        official_url,
                        contact,
                        last_verified_at,
                        is_active,
                        updated_at
                    )
                    SELECT
                        import_id,
                        serv_id,
                        COALESCE(
                            NULLIF(detail_json->>'servNm', ''),
                            NULLIF(list_json->>'servNm', ''),
                            serv_id
                        ),
                        NULLIF(
                            split_part(
                                COALESCE(
                                    NULLIF(detail_json->>'intrsThemaArray', ''),
                                    NULLIF(list_json->>'intrsThemaArray', '')
                                ),
                                ',',
                                1
                            ),
                            ''
                        ),
                        NULLIF(
                            split_part(
                                COALESCE(
                                    NULLIF(detail_json->>'lifeArray', ''),
                                    NULLIF(list_json->>'lifeArray', '')
                                ),
                                ',',
                                1
                            ),
                            ''
                        ),
                        COALESCE(
                            NULLIF(detail_json->>'jurMnofNm', ''),
                            NULLIF(list_json->>'jurMnofNm', '')
                        ),
                        NULLIF(list_json->>'jurOrgNm', ''),
                        'NATIONAL',
                        NULL,
                        COALESCE(
                            NULLIF(detail_json->>'srvPvsnNm', ''),
                            NULLIF(list_json->>'srvPvsnNm', ''),
                            NULLIF(list_json->>'sprtCycNm', '')
                        ),
                        CASE NULLIF(list_json->>'onapPsbltYn', '')
                            WHEN 'Y' THEN 'ONLINE_AVAILABLE'
                            WHEN 'N' THEN 'OFFLINE_ONLY'
                            ELSE NULL
                        END,
                        COALESCE(
                            NULLIF(list_json->>'servDtlLink', ''),
                            (
                                SELECT NULLIF(link_item->>'servSeDetailLink', '')
                                FROM jsonb_array_elements(
                                    COALESCE(detail_json->'inqplHmpgReldList', '[]'::jsonb)
                                ) AS link_item
                                WHERE NULLIF(link_item->>'servSeDetailLink', '') IS NOT NULL
                                LIMIT 1
                            )
                        ),
                        COALESCE(
                            NULLIF(detail_json->>'rprsCtadr', ''),
                            NULLIF(list_json->>'rprsCtadr', ''),
                            (
                                SELECT string_agg(
                                    concat_ws(': ',
                                        NULLIF(contact_item->>'servSeDetailNm', ''),
                                        NULLIF(contact_item->>'servSeDetailLink', '')
                                    ),
                                    E'\n'
                                    ORDER BY contact_item->>'servSeDetailNm'
                                )
                                FROM jsonb_array_elements(
                                    COALESCE(detail_json->'inqplCtadrList', '[]'::jsonb)
                                ) AS contact_item
                            )
                        ),
                        detail_fetched_at,
                        TRUE,
                        CURRENT_TIMESTAMP
                    FROM raw
                    ON CONFLICT (policy_code) DO UPDATE SET
                        policy_name = EXCLUDED.policy_name,
                        main_category = EXCLUDED.main_category,
                        sub_category = EXCLUDED.sub_category,
                        provider_name = EXCLUDED.provider_name,
                        provider_type = EXCLUDED.provider_type,
                        region_scope = EXCLUDED.region_scope,
                        region_code = EXCLUDED.region_code,
                        benefit_type = EXCLUDED.benefit_type,
                        application_status = EXCLUDED.application_status,
                        official_url = EXCLUDED.official_url,
                        contact = EXCLUDED.contact,
                        last_verified_at = EXCLUDED.last_verified_at,
                        is_active = EXCLUDED.is_active,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING policy_id
                )
                SELECT COUNT(*) FROM upserted
            """,
        )

    @classmethod
    async def upsert_policy_details(cls, conn) -> int:
        return await cls._fetch_count(
            conn,
            """
                WITH raw AS (
                    SELECT
                        p.policy_id,
                        r.list_json,
                        r.detail_json
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    WHERE r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'
                ),
                upserted AS (
                    INSERT INTO policy_detail (
                        policy_id,
                        easy_summary,
                        target_description,
                        benefit_description,
                        application_method,
                        application_period_text,
                        caution
                    )
                    SELECT
                        policy_id,
                        COALESCE(
                            NULLIF(detail_json->>'wlfareInfoOutlCn', ''),
                            NULLIF(list_json->>'servDgst', '')
                        ),
                        NULLIF(detail_json->>'tgtrDtlCn', ''),
                        NULLIF(detail_json->>'alwServCn', ''),
                        (
                            SELECT string_agg(
                                concat_ws(': ',
                                    NULLIF(method_item->>'servSeDetailNm', ''),
                                    NULLIF(method_item->>'servSeDetailLink', '')
                                ),
                                E'\n'
                                ORDER BY method_item->>'servSeDetailNm',
                                         method_item->>'servSeDetailLink'
                            )
                            FROM jsonb_array_elements(
                                COALESCE(detail_json->'applmetList', '[]'::jsonb)
                            ) AS method_item
                        ),
                        NULLIF(list_json->>'sprtCycNm', ''),
                        NULLIF(detail_json->>'slctCritCn', '')
                    FROM raw
                    ON CONFLICT (policy_id) DO UPDATE SET
                        easy_summary = EXCLUDED.easy_summary,
                        target_description = EXCLUDED.target_description,
                        benefit_description = EXCLUDED.benefit_description,
                        application_method = EXCLUDED.application_method,
                        application_period_text = EXCLUDED.application_period_text,
                        caution = EXCLUDED.caution
                    RETURNING policy_id
                )
                SELECT COUNT(*) FROM upserted
            """,
        )

    # ============================================================
    # 2. required_document / policy_document 저장
    # ============================================================
    @classmethod
    async def replace_required_documents(cls, conn) -> int:
        await cls._execute(
            conn,
            """
                DELETE FROM required_document d
                USING policy_raw_import r
                JOIN policy p ON p.policy_code = r.serv_id
                WHERE d.policy_id = p.policy_id
                  AND d.source_type = 'POLICY_REFERENCE'
                  AND r.list_json IS NOT NULL
                  AND r.detail_json IS NOT NULL
                  AND r.detail_status = 'COMPLETED'
            """,
        )
        return await cls._fetch_count(
            conn,
            """
                WITH document_items AS (
                    SELECT
                        p.policy_id,
                        form_item.ordinality::int AS item_order,
                        NULLIF(form_item.item->>'servSeDetailNm', '')
                            AS document_name,
                        NULLIF(form_item.item->>'servSeDetailLink', '')
                            AS document_link
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    CROSS JOIN LATERAL jsonb_array_elements(
                        COALESCE(r.detail_json->'basfrmList', '[]'::jsonb)
                    ) WITH ORDINALITY AS form_item(item, ordinality)
                    WHERE r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'
                ),
                normalized_documents AS (
                    SELECT
                        (policy_id * 10000 + item_order)::bigint
                            AS required_document_id,
                        policy_id,
                        document_name,
                        'REQUIRED' AS required_type,
                        NULL::text AS issue_place,
                        document_link AS description
                    FROM document_items
                    WHERE document_name IS NOT NULL
                      AND (
                        document_name LIKE '%신청%서%'
                        OR document_name LIKE '%동의서%'
                        OR document_name LIKE '%확인서%'
                        OR document_name LIKE '%위임장%'
                        OR document_name LIKE '%진단서%'
                        OR document_name LIKE '%증명서%'
                      )
                ),
                inserted AS (
                    INSERT INTO required_document (
                        required_document_id,
                        policy_id,
                        document_name,
                        required_type,
                        issue_place,
                        description
                    )
                    SELECT
                        required_document_id,
                        policy_id,
                        document_name,
                        required_type,
                        issue_place,
                        description
                    FROM normalized_documents
                    RETURNING required_document_id
                )
                SELECT COUNT(*) FROM inserted
            """,
        )

    @classmethod
    async def replace_policy_documents(cls, conn) -> int:
        """Version reference documents instead of deleting their evidence history.

        A raw-data refresh must never remove POLICY_DETAIL documents or chunks
        that existing chat messages cite.  Changed reference URLs/titles are
        superseded and a new current document is created for ingestion.
        """
        await cls._execute(
            conn,
            """
                WITH document_items AS (
                    SELECT
                        p.policy_id,
                        NULLIF(form_item.item->>'servSeDetailNm', '')
                            AS source_title,
                        NULLIF(form_item.item->>'servSeDetailLink', '')
                            AS source_url
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    CROSS JOIN LATERAL jsonb_array_elements(
                        COALESCE(r.detail_json->'basfrmList', '[]'::jsonb)
                    ) AS form_item(item)
                    WHERE r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'
                ),
                current_fingerprints AS (
                    SELECT
                        policy_id,
                        md5(concat_ws('|', policy_id::text, 'POLICY_REFERENCE',
                            source_url, source_title)) AS source_fingerprint
                    FROM document_items
                    WHERE source_title IS NOT NULL
                )
                UPDATE policy_document d
                SET is_current = FALSE,
                    superseded_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE d.source_type = 'POLICY_REFERENCE'
                  AND d.is_current = TRUE
                  AND EXISTS (
                    SELECT 1
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    WHERE p.policy_id = d.policy_id
                      AND r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM current_fingerprints f
                    WHERE f.policy_id = d.policy_id
                      AND f.source_fingerprint = d.source_fingerprint
                  )
            """,
        )
        # Keep historical policy_document/chunk rows for chat evidence, but
        # remove their vectors immediately so retrieval cannot return a
        # superseded source revision. PGVector may not be initialized yet.
        await cls._execute(
            conn,
            """
                DO $$
                BEGIN
                    IF to_regclass('public.langchain_pg_collection') IS NOT NULL
                       AND to_regclass('public.langchain_pg_embedding') IS NOT NULL THEN
                        DELETE FROM langchain_pg_embedding embedding
                        USING langchain_pg_collection collection,
                              policy_document document
                        WHERE embedding.collection_id = collection.uuid
                          AND collection.name = 'policy_documents'
                          AND document.is_current = FALSE
                          AND document.superseded_at = transaction_timestamp()
                          AND (
                              embedding.cmetadata->>'document_id'
                                  = document.document_id::text
                              OR embedding.id IN (
                                  SELECT chunk_id::text
                                  FROM policy_document_chunk
                                  WHERE document_id = document.document_id
                              )
                          );
                    END IF;
                END $$;
            """,
        )

        return await cls._fetch_count(
            conn,
            """
                WITH document_items AS (
                    SELECT
                        p.policy_id,
                        form_item.ordinality::int AS item_order,
                        NULLIF(form_item.item->>'servSeDetailNm', '')
                            AS source_title,
                        NULLIF(form_item.item->>'servSeDetailLink', '')
                            AS source_url
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    CROSS JOIN LATERAL jsonb_array_elements(
                        COALESCE(r.detail_json->'basfrmList', '[]'::jsonb)
                    ) WITH ORDINALITY AS form_item(item, ordinality)
                    WHERE r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'
                ),
                normalized_documents AS (
                    SELECT DISTINCT
                        policy_id,
                        source_title,
                        source_url,
                        md5(concat_ws('|', policy_id::text, 'POLICY_REFERENCE',
                            source_url, source_title)) AS source_fingerprint
                    FROM document_items
                    WHERE source_title IS NOT NULL
                      AND NOT (
                        source_title LIKE '%신청%서%'
                        OR source_title LIKE '%동의서%'
                        OR source_title LIKE '%확인서%'
                        OR source_title LIKE '%위임장%'
                        OR source_title LIKE '%진단서%'
                        OR source_title LIKE '%증명서%'
                      )
                ),
                inserted AS (
                    INSERT INTO policy_document (
                        policy_id,
                        source_title,
                        source_url,
                        source_type,
                        raw_text,
                        source_fingerprint,
                        ingest_status,
                        is_current,
                        updated_at
                    )
                    SELECT
                        policy_id,
                        source_title,
                        source_url,
                        'POLICY_REFERENCE',
                        NULL,
                        source_fingerprint,
                        'PENDING_TEXT',
                        TRUE,
                        CURRENT_TIMESTAMP
                    FROM normalized_documents
                    ON CONFLICT (policy_id, source_fingerprint) WHERE is_current
                    DO UPDATE SET
                        source_title = EXCLUDED.source_title,
                        source_url = EXCLUDED.source_url,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING document_id
                )
                SELECT COUNT(*) FROM inserted
            """,
        )

    # ============================================================
    # 3. tag / checklist 저장
    # ============================================================
    @classmethod
    async def replace_policy_tags(cls, conn) -> int:
        await cls._execute(
            conn,
            """
                DELETE FROM policy_tag t
                USING policy_raw_import r
                JOIN policy p ON p.policy_code = r.serv_id
                WHERE t.policy_id = p.policy_id
                  AND r.list_json IS NOT NULL
                  AND r.detail_json IS NOT NULL
                  AND r.detail_status = 'COMPLETED'
            """,
        )

        return await cls._fetch_count(
            conn,
            """
                WITH raw_tags AS (
                    SELECT
                        p.policy_id,
                        regexp_split_to_table(
                            concat_ws(
                                ',',
                                NULLIF(COALESCE(r.detail_json->>'lifeArray', r.list_json->>'lifeArray'), ''),
                                NULLIF(COALESCE(
                                    r.detail_json->>'trgterIndvdlArray',
                                    r.list_json->>'trgterIndvdlArray'
                                ), ''),
                                NULLIF(COALESCE(
                                    r.detail_json->>'intrsThemaArray',
                                    r.list_json->>'intrsThemaArray'
                                ), '')
                            ),
                            ','
                        ) AS tag_name
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    WHERE r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'
                ),
                normalized_tags AS (
                    SELECT DISTINCT
                        policy_id,
                        trim(tag_name) AS tag_name
                    FROM raw_tags
                    WHERE trim(tag_name) <> ''
                ),
                inserted AS (
                    INSERT INTO policy_tag (policy_id, tag_name)
                    SELECT policy_id, tag_name
                    FROM normalized_tags
                    ON CONFLICT (policy_id, tag_name) DO NOTHING
                    RETURNING policy_id
                )
                SELECT COUNT(*) FROM inserted
            """,
        )

    @classmethod
    async def replace_policy_rules(cls, conn) -> int:
        await cls._execute(
            conn,
            """
                CREATE TABLE IF NOT EXISTS policy_rule (
                    rule_id bigserial PRIMARY KEY,
                    policy_id bigint NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
                    rule_type varchar(50) NOT NULL,
                    operator varchar(30) NOT NULL,
                    field_name varchar(100) NOT NULL,
                    value_json jsonb NOT NULL,
                    is_hard_filter boolean NOT NULL DEFAULT true,
                    manual_check_required boolean NOT NULL DEFAULT false,
                    manual_check_reason text,
                    note text,
                    rule_group varchar(100) NOT NULL DEFAULT 'ALL',
                    group_operator varchar(10) NOT NULL DEFAULT 'AND',
                    source_text text,
                    confidence numeric(5, 4),
                    review_required boolean NOT NULL DEFAULT false,
                    is_exclusion boolean NOT NULL DEFAULT false
                )
            """,
        )
        for statement in [
            "ALTER TABLE policy_rule ADD COLUMN IF NOT EXISTS rule_group varchar(100) NOT NULL DEFAULT 'ALL'",
            "ALTER TABLE policy_rule ADD COLUMN IF NOT EXISTS group_operator varchar(10) NOT NULL DEFAULT 'AND'",
            "ALTER TABLE policy_rule ADD COLUMN IF NOT EXISTS source_text text",
            "ALTER TABLE policy_rule ADD COLUMN IF NOT EXISTS confidence numeric(5, 4)",
            "ALTER TABLE policy_rule ADD COLUMN IF NOT EXISTS review_required boolean NOT NULL DEFAULT false",
            "ALTER TABLE policy_rule ADD COLUMN IF NOT EXISTS is_exclusion boolean NOT NULL DEFAULT false",
            "ALTER TABLE policy_rule DROP CONSTRAINT IF EXISTS policy_rule_group_operator_check",
            """
                ALTER TABLE policy_rule
                ADD CONSTRAINT policy_rule_group_operator_check
                CHECK (group_operator IN ('AND', 'OR'))
            """,
        ]:
            await cls._execute(conn, statement)
        await cls._execute(
            conn,
            """
                CREATE INDEX IF NOT EXISTS policy_rule_policy_id_idx
                ON policy_rule (policy_id)
            """,
        )
        await cls._execute(
            conn,
            """
                CREATE INDEX IF NOT EXISTS policy_rule_policy_group_idx
                ON policy_rule (policy_id, rule_group, group_operator)
            """,
        )
        await cls._execute(
            conn,
            """
                DELETE FROM policy_rule pr
                USING policy_raw_import r
                JOIN policy p ON p.policy_code = r.serv_id
                WHERE pr.policy_id = p.policy_id
                  AND r.list_json IS NOT NULL
                  AND r.detail_json IS NOT NULL
                  AND r.detail_status = 'COMPLETED'
            """,
        )

        return await cls._fetch_count(
            conn,
            """
                WITH raw AS (
                    SELECT
                        p.policy_id,
                        COALESCE(
                            NULLIF(r.detail_json->>'lifeArray', ''),
                            NULLIF(r.list_json->>'lifeArray', '')
                        ) AS life_array,
                        COALESCE(
                            NULLIF(r.detail_json->>'trgterIndvdlArray', ''),
                            NULLIF(r.list_json->>'trgterIndvdlArray', '')
                        ) AS special_array,
                        concat_ws(
                            E'\n',
                            NULLIF(r.detail_json->>'tgtrDtlCn', ''),
                            NULLIF(r.detail_json->>'slctCritCn', '')
                        ) AS condition_text
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    WHERE r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'
                ),
                stage_source AS (
                    SELECT
                        policy_id,
                        trim(tag_name) AS tag_name
                    FROM raw
                    CROSS JOIN LATERAL regexp_split_to_table(
                        COALESCE(life_array, ''),
                        ','
                    ) AS tag_name
                    WHERE trim(tag_name) <> ''
                ),
                stage_rules AS (
                    SELECT DISTINCT
                        policy_id,
                        'AGE' AS rule_type,
                        'IN' AS operator,
                        'stage' AS field_name,
                        CASE
                            WHEN tag_name LIKE '%임신%' OR tag_name LIKE '%출산%'
                                THEN '["pregnant"]'::jsonb
                            WHEN tag_name LIKE '%영유아%'
                                THEN '["newborn", "infant"]'::jsonb
                            WHEN tag_name LIKE '%아동%'
                                THEN '["child"]'::jsonb
                            WHEN tag_name LIKE '%청소년%'
                                THEN '["teen"]'::jsonb
                            ELSE NULL
                        END AS value_json,
                        TRUE AS is_hard_filter,
                        FALSE AS manual_check_required,
                        NULL::text AS manual_check_reason,
                        'lifeArray: ' || tag_name AS note
                    FROM stage_source
                ),
                special_source AS (
                    SELECT
                        policy_id,
                        trim(tag_name) AS tag_name
                    FROM raw
                    CROSS JOIN LATERAL regexp_split_to_table(
                        COALESCE(special_array, ''),
                        ','
                    ) AS tag_name
                    WHERE trim(tag_name) <> ''
                ),
                special_rules AS (
                    SELECT DISTINCT
                        policy_id,
                        CASE
                            WHEN tag_name LIKE '%저소득%' THEN 'INCOME'
                            ELSE 'HOUSEHOLD'
                        END AS rule_type,
                        'IN' AS operator,
                        'special' AS field_name,
                        CASE
                            WHEN tag_name LIKE '%한부모%' OR tag_name LIKE '%조손%'
                                THEN '["single"]'::jsonb
                            WHEN tag_name LIKE '%다문화%' OR tag_name LIKE '%탈북%'
                                THEN '["multi"]'::jsonb
                            WHEN tag_name LIKE '%장애%'
                                THEN '["disabled"]'::jsonb
                            WHEN tag_name LIKE '%다자녀%'
                                THEN '["many"]'::jsonb
                            WHEN tag_name LIKE '%저소득%'
                                THEN '["low_income"]'::jsonb
                            WHEN tag_name LIKE '%보훈%'
                                THEN '["veteran"]'::jsonb
                            ELSE NULL
                        END AS value_json,
                        TRUE AS is_hard_filter,
                        FALSE AS manual_check_required,
                        NULL::text AS manual_check_reason,
                        'trgterIndvdlArray: ' || tag_name AS note
                    FROM special_source
                ),
                income_rules AS (
                    SELECT DISTINCT
                        policy_id,
                        'INCOME' AS rule_type,
                        'LTE' AS operator,
                        'income' AS field_name,
                        jsonb_build_object(
                            'value',
                            COALESCE(
                                substring(
                                    condition_text
                                    FROM '기준\\s*중위소득\\s*([0-9]{2,3})\\s*%?\\s*(이하|미만|이내|내)'
                                ),
                                substring(
                                    condition_text
                                    FROM '중위소득\\s*([0-9]{2,3})\\s*%?\\s*(이하|미만|이내|내)'
                                )
                            )::int
                        ) AS value_json,
                        TRUE AS is_hard_filter,
                        FALSE AS manual_check_required,
                        NULL::text AS manual_check_reason,
                        concat(
                            '중위소득 ',
                            COALESCE(
                                substring(
                                    condition_text
                                    FROM '기준\\s*중위소득\\s*([0-9]{2,3})\\s*%?\\s*(이하|미만|이내|내)'
                                ),
                                substring(
                                    condition_text
                                    FROM '중위소득\\s*([0-9]{2,3})\\s*%?\\s*(이하|미만|이내|내)'
                                )
                            ),
                            '% 이하'
                        ) AS note
                    FROM raw
                    WHERE COALESCE(
                        substring(
                            condition_text
                            FROM '기준\\s*중위소득\\s*([0-9]{2,3})\\s*%?\\s*(이하|미만|이내|내)'
                        ),
                        substring(
                            condition_text
                            FROM '중위소득\\s*([0-9]{2,3})\\s*%?\\s*(이하|미만|이내|내)'
                        )
                    ) IS NOT NULL
                ),
                low_income_manual_rules AS (
                    SELECT DISTINCT
                        policy_id,
                        'INCOME' AS rule_type,
                        'EXISTS' AS operator,
                        'income' AS field_name,
                        jsonb_build_object('keyword', '저소득/수급 자격') AS value_json,
                        FALSE AS is_hard_filter,
                        TRUE AS manual_check_required,
                        '저소득/수급 자격 문구는 있으나 정확한 중위소득 기준이 없습니다.' AS manual_check_reason,
                        '저소득/수급 자격 관련 조건' AS note
                    FROM raw
                    WHERE COALESCE(
                        substring(
                            condition_text
                            FROM '기준\\s*중위소득\\s*([0-9]{2,3})\\s*%?\\s*(이하|미만|이내|내)'
                        ),
                        substring(
                            condition_text
                            FROM '중위소득\\s*([0-9]{2,3})\\s*%?\\s*(이하|미만|이내|내)'
                        )
                    ) IS NULL
                      AND (
                        condition_text LIKE '%저소득%'
                        OR condition_text LIKE '%기초생활%'
                        OR condition_text LIKE '%차상위%'
                        OR condition_text LIKE '%수급%'
                        OR condition_text LIKE '%의료급여%'
                      )
                ),
                child_age_rules AS (
                    SELECT DISTINCT
                        policy_id,
                        'AGE' AS rule_type,
                        'IN' AS operator,
                        'childAge' AS field_name,
                        CASE
                            WHEN condition_text ~ '0\\s*[~\\-∼]\\s*5\\s*세'
                                OR condition_text ~ '만\\s*5\\s*세\\s*(이하|미만)'
                                OR condition_text LIKE '%영유아%'
                                THEN '["0", "1", "2-5"]'::jsonb
                            WHEN condition_text ~ '만\\s*0\\s*세'
                                OR condition_text ~ '(^|[^0-9])0\\s*세'
                                THEN '["0"]'::jsonb
                            WHEN condition_text ~ '만\\s*1\\s*세'
                                OR condition_text ~ '(^|[^0-9])1\\s*세'
                                THEN '["1"]'::jsonb
                            WHEN condition_text ~ '6\\s*[~\\-∼]\\s*12\\s*세'
                                OR condition_text ~ '만\\s*12\\s*세\\s*(이하|미만)'
                                OR condition_text LIKE '%아동%'
                                THEN '["6-12"]'::jsonb
                            WHEN condition_text ~ '만\\s*13\\s*세\\s*(이상|초과)'
                                OR condition_text LIKE '%청소년%'
                                THEN '["13+"]'::jsonb
                            ELSE NULL
                        END AS value_json,
                        TRUE AS is_hard_filter,
                        FALSE AS manual_check_required,
                        NULL::text AS manual_check_reason,
                        '대상/선정기준 연령 조건' AS note
                    FROM raw
                    WHERE condition_text IS NOT NULL
                      AND btrim(condition_text) <> ''
                ),
                normalized_rules AS (
                    SELECT * FROM stage_rules WHERE value_json IS NOT NULL
                    UNION
                    SELECT * FROM special_rules WHERE value_json IS NOT NULL
                    UNION
                    SELECT * FROM income_rules WHERE value_json IS NOT NULL
                    UNION
                    SELECT * FROM low_income_manual_rules WHERE value_json IS NOT NULL
                    UNION
                    SELECT * FROM child_age_rules WHERE value_json IS NOT NULL
                ),
                inserted AS (
                    INSERT INTO policy_rule (
                        policy_id,
                        rule_type,
                        operator,
                        field_name,
                        value_json,
                        is_hard_filter,
                        manual_check_required,
                        manual_check_reason,
                        note
                    )
                    SELECT
                        policy_id,
                        rule_type,
                        operator,
                        field_name,
                        value_json,
                        is_hard_filter,
                        manual_check_required,
                        manual_check_reason,
                        note
                    FROM normalized_rules
                    RETURNING rule_id
                )
                SELECT COUNT(*) FROM inserted
            """,
        )

    @classmethod
    async def replace_policy_checklist_templates(cls, conn) -> int:
        return await cls._fetch_count(
            conn,
            """
                WITH import_policies AS (
                    SELECT p.policy_id
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    WHERE r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'
                ),
                checklist_items AS (
                    SELECT
                        p.policy_id,
                        'APPLY_STEP_' || lpad(step_item.ordinality::text, 3, '0')
                            AS item_code,
                        NULLIF(step_item.item->>'servSeDetailNm', '') AS item_label,
                        NULLIF(step_item.item->>'servSeDetailLink', '')
                            AS item_description,
                        TRUE AS is_required,
                        100 + step_item.ordinality::int AS display_order
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    CROSS JOIN LATERAL jsonb_array_elements(
                        COALESCE(r.detail_json->'applmetList', '[]'::jsonb)
                    ) WITH ORDINALITY AS step_item(item, ordinality)
                    WHERE r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'

                    UNION ALL

                    SELECT
                        p.policy_id,
                        'FORM_' || lpad(form_item.ordinality::text, 3, '0')
                            AS item_code,
                        NULLIF(form_item.item->>'servSeDetailNm', '') AS item_label,
                        NULLIF(form_item.item->>'servSeDetailLink', '')
                            AS item_description,
                        TRUE AS is_required,
                        200 + form_item.ordinality::int AS display_order
                    FROM policy_raw_import r
                    JOIN policy p ON p.policy_code = r.serv_id
                    CROSS JOIN LATERAL jsonb_array_elements(
                        COALESCE(r.detail_json->'basfrmList', '[]'::jsonb)
                    ) WITH ORDINALITY AS form_item(item, ordinality)
                    WHERE r.list_json IS NOT NULL
                      AND r.detail_json IS NOT NULL
                      AND r.detail_status = 'COMPLETED'
                      AND (
                        form_item.item->>'servSeDetailNm' LIKE '%신청%서%'
                        OR form_item.item->>'servSeDetailNm' LIKE '%동의서%'
                        OR form_item.item->>'servSeDetailNm' LIKE '%확인서%'
                        OR form_item.item->>'servSeDetailNm' LIKE '%위임장%'
                        OR form_item.item->>'servSeDetailNm' LIKE '%진단서%'
                        OR form_item.item->>'servSeDetailNm' LIKE '%증명서%'
                      )
                ),
                normalized_items AS (
                    SELECT
                        (policy_id * 10000 + display_order)::bigint
                            AS template_item_id,
                        policy_id,
                        item_code,
                        item_label,
                        item_description,
                        is_required,
                        display_order
                    FROM checklist_items
                    WHERE item_label IS NOT NULL
                ),
                deactivated AS (
                    UPDATE policy_checklist_template t
                    SET
                        is_active = FALSE,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE t.policy_id IN (SELECT policy_id FROM import_policies)
                      AND t.template_item_id NOT IN (
                        SELECT template_item_id FROM normalized_items
                      )
                      AND t.is_active = TRUE
                    RETURNING t.template_item_id
                ),
                upserted AS (
                    INSERT INTO policy_checklist_template (
                        template_item_id,
                        policy_id,
                        item_code,
                        item_label,
                        item_description,
                        is_required,
                        display_order,
                        is_active,
                        updated_at
                    )
                    SELECT
                        template_item_id,
                        policy_id,
                        item_code,
                        item_label,
                        item_description,
                        is_required,
                        display_order,
                        TRUE,
                        CURRENT_TIMESTAMP
                    FROM normalized_items
                    ON CONFLICT (template_item_id) DO UPDATE SET
                        policy_id = EXCLUDED.policy_id,
                        item_code = EXCLUDED.item_code,
                        item_label = EXCLUDED.item_label,
                        item_description = EXCLUDED.item_description,
                        is_required = EXCLUDED.is_required,
                        display_order = EXCLUDED.display_order,
                        is_active = TRUE,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING template_item_id
                )
                SELECT COUNT(*) FROM upserted
            """,
        )

    # ============================================================
    # 4. import 대상 조회
    # ============================================================
    @classmethod
    async def count_importable_raw_rows(cls, conn) -> int:
        return await cls._fetch_count(
            conn,
            """
                SELECT COUNT(*)
                FROM policy_raw_import
                WHERE list_json IS NOT NULL
                  AND detail_json IS NOT NULL
                  AND detail_status = 'COMPLETED'
            """,
        )
