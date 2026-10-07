Project policy_rag_agent_platform {
database_type: "PostgreSQL"
Note: "Dodam integrated schema for policy recommendation, chat session history, RAG evidence, eligibility assessment, and user policy checklist progress"
}

Table users {
user_id bigint [pk, increment]
email varchar(255) [not null, unique]
password_hash varchar(255) [not null]
nickname varchar(100) [not null, unique]
role varchar(20) [not null, default: 'USER']
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table user_profile {
profile_id bigint [pk, increment]
user_id bigint [not null, unique]
region_code varchar(50)
household_type varchar(50)
income_bracket varchar(50)
employment_status varchar(50)
pregnancy_status boolean [not null, default: false]
profile_json jsonb [note: 'stable user profile only']
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table family_member {
family_member_id bigint [pk, increment]
user_id bigint [not null]
relation varchar(50) [not null]
birth_year integer
life_stage varchar(50)
note text
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table policy {
policy_id bigint [pk, increment]
policy_code varchar(100) [not null, unique]
policy_name varchar(255) [not null]
main_category varchar(100)
sub_category varchar(100)
provider_name varchar(255)
provider_type varchar(50)
region_scope varchar(50) [note: 'NATIONAL, LOCAL']
region_code varchar(50)
benefit_type varchar(50)
application_status varchar(50)
application_start_date date
application_end_date date
official_url text
contact varchar(255)
last_verified_at timestamp
is_active boolean [not null, default: true]
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table policy_detail {
policy_id bigint [pk]
easy_summary text
target_description text
benefit_description text
application_method text
application_period_text text
caution text
}

Table policy_tag {
policy_id bigint [not null]
tag_name varchar(100) [not null]

indexes {
(policy_id, tag_name) [pk]
}
}

Table policy_rule {
rule_id bigint [pk, increment]
policy_id bigint [not null]
rule_type varchar(50) [not null, note: 'AGE, REGION, INCOME, HOUSEHOLD, EMPLOYMENT, CHILD, EXCLUSION']
operator varchar(30) [not null, note: 'EQ, IN, GTE, LTE, EXISTS']
field_name varchar(100) [not null]
value_json jsonb [not null]
is_hard_filter boolean [not null, default: true]
manual_check_required boolean [not null, default: false]
manual_check_reason text
note text
}

Table required_document {
required_document_id bigint [pk, increment]
policy_id bigint [not null]
document_name varchar(255) [not null]
required_type varchar(50)
issue_place varchar(255)
description text
}

Table policy_document {
document_id bigint [pk, increment]
policy_id bigint [not null]
source_title varchar(255)
source_url text
source_type varchar(50) [note: 'PDF, HTML, NOTICE']
raw_text text
source_fingerprint varchar(64) [note: 'source URL/title version identity']
content_hash varchar(64) [note: 'extracted raw_text hash']
ingest_status varchar(30) [note: 'PENDING_TEXT, TEXT_READY, CHUNK_READY, EMBEDDED']
ingest_error text
embedded_metadata_version varchar(30)
is_current boolean [not null, default: true]
superseded_at timestamp
collected_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table policy_document_chunk {
chunk_id bigint [pk, increment]
document_id bigint [not null]
chunk_index integer [not null]
chunk_text text [not null]
metadata_json jsonb

indexes {
(document_id, chunk_index) [unique]
}

Table source_snapshot {
snapshot_id bigint [pk, increment]
source_name varchar(100) [not null]
source_locator text [not null]
source_fingerprint varchar(64) [not null]
payload_hash varchar(64) [not null]
mapping_version varchar(50) [not null]
fetched_at timestamp [not null]
}

Table ingestion_attempt {
attempt_id bigint [pk, increment]
snapshot_id bigint [not null]
stage varchar(50) [not null]
status varchar(30) [not null]
error_message text
counters_json jsonb [not null]
started_at timestamp [not null]
finished_at timestamp
}

Table domain_entity {
entity_id bigint [pk, increment]
entity_type varchar(100) [not null]
canonical_key varchar(255) [not null]
display_name text [not null]
attributes_json jsonb [not null]
status varchar(30) [not null]
}

Table domain_relation {
relation_id bigint [pk, increment]
subject_entity_id bigint [not null]
predicate varchar(100) [not null]
object_entity_id bigint [not null]
attributes_json jsonb [not null]
confidence decimal(5,4)
review_status varchar(30) [not null]
}

Table evidence_span {
evidence_span_id bigint [pk, increment]
document_id bigint
chunk_id bigint
snapshot_id bigint
char_start integer
char_end integer
quoted_text text [not null]
content_hash varchar(64) [not null]
}

Table decision_run {
decision_run_id bigint [pk, increment]
decision_type varchar(50) [not null]
subject_ref varchar(255) [not null]
input_snapshot_json jsonb [not null]
status varchar(30) [not null]
model_or_rule_version varchar(100)
}

Table decision_claim {
decision_claim_id bigint [pk, increment]
decision_run_id bigint [not null]
claim_type varchar(100) [not null]
claim_text text [not null]
confidence decimal(5,4)
review_status varchar(30) [not null]
}
}

Table chat_session {
chat_session_id bigint [pk, increment]
user_id bigint [not null]
title varchar(255)
session_status varchar(30) [not null, default: 'ACTIVE']
last_message_at timestamp
latest_request_id bigint
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table chat_message {
chat_message_id bigint [pk, increment]
chat_session_id bigint [not null]
parent_message_id bigint
role varchar(20) [not null, note: 'USER, ASSISTANT, SYSTEM, TOOL']
message_type varchar(30) [not null, default: 'TEXT']
content text
structured_json jsonb [note: 'tool output, parsed conditions, UI action payloads']
sequence_no integer [not null]
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]

indexes {
(chat_session_id, sequence_no) [unique]
}
}

Table recommendation_request {
request_id bigint [pk, increment]
user_id bigint [not null]
source_type varchar(30) [not null, default: 'FORM', note: 'FORM, CHAT, POLICY_DETAIL, COMPARE']
source_ref_id varchar(100) [note: 'loose source reference such as chat_message:123 or WLF00004611']
raw_query text
parsed_query_json jsonb [note: 'input parsing result before profile merge']
merged_condition_json jsonb [note: 'normalized final condition set after profile merge']
profile_conflict_json jsonb [note: 'conflicts between stored profile and current input']
result_json jsonb [note: 'request-scoped recommendation results and evidence summary']
request_status varchar(50) [not null, default: 'READY']
error_message text
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table eligibility_request {
request_id bigint [pk, increment]
user_id bigint [not null]
policy_id bigint [not null]
source_type varchar(30) [not null, default: 'POLICY_DETAIL', note: 'POLICY_DETAIL, CHAT, FORM']
source_ref_id varchar(100) [note: 'loose source reference such as chat_message:123 or WLF00004611']
raw_query text
parsed_query_json jsonb [note: 'input parsing result before profile merge']
merged_condition_json jsonb [note: 'normalized final condition set after profile merge']
profile_conflict_json jsonb [note: 'conflicts between stored profile and current input']
request_status varchar(50) [not null, default: 'READY']
error_message text
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table follow_up_question {
follow_up_id bigint [pk, increment]
request_id bigint [not null]
field_name varchar(100) [not null]
question_text text [not null]
reason text
answer_value_json jsonb
answer_message_id bigint
answered_at timestamp
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table recommendation_candidate {
candidate_id bigint [pk, increment]
request_id bigint [not null]
policy_id bigint [not null]
filter_match_json jsonb [note: 'why candidate survived DB filtering']
retrieval_score decimal(10,4)
rerank_score decimal(10,4)
candidate_status varchar(50) [not null, default: 'CANDIDATE']
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]

indexes {
(request_id, policy_id) [unique]
}
}

Table policy_assessment {
assessment_id bigint [pk, increment]
request_id bigint [note: 'legacy recommendation_request id, nullable for eligibility_detail']
recommendation_request_id bigint
eligibility_request_id bigint
policy_id bigint [not null]

assessment_type varchar(50) [not null, note: 'recommendation_assessment, eligibility_detail']

assessment_status varchar(50) [not null, note: 'LIKELY_MATCH, NEEDS_MORE_INFO, NOT_MATCH, INSUFFICIENT_PROFILE, CONFLICTING_PROFILE']
confidence_score decimal(5,2)

matched_conditions_json jsonb
missing_conditions_json jsonb
conflicting_conditions_json jsonb
manual_check_points_json jsonb
reason_summary text

selected_for_result boolean [not null, default: false]
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]

indexes {
(request_id, policy_id, assessment_type) [unique]
(recommendation_request_id, policy_id, assessment_type)
(eligibility_request_id, policy_id, assessment_type)
}
}
Table assessment_evidence {
evidence_id bigint [pk, increment]
assessment_id bigint [not null]
chunk_id bigint [not null]
snippet text
similarity_score decimal(10,4)
evidence_role varchar(50) [note: 'SUMMARY, TARGET, BENEFIT, APPLICATION, CAUTION. API 응답은 소문자로 직렬화. RAG가 role을 분류한 경우에만 값을 채우며 nullable.']
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table chat_message_policy {
chat_message_policy_id bigint [pk, increment]
chat_message_id bigint [not null]
policy_id bigint [not null]
action_type varchar(30) [not null, note: 'RECOMMENDED, COMPARED, ELIGIBILITY_TARGET, APPLY_TARGET. intent별 매핑: recommend→RECOMMENDED, compare→COMPARED, eligibility→ELIGIBILITY_TARGET, apply→APPLY_TARGET. policy_summary intent는 row를 생성하지 않고 거론된 정책은 chat_message_evidence를 통해 역추적한다.']
}

Table chat_message_evidence {
chat_message_evidence_id bigint [pk, increment]
chat_message_id bigint [not null]
chunk_id bigint [not null]
snippet text
evidence_role varchar(30) [note: 'SUMMARY, TARGET, BENEFIT, APPLICATION, CAUTION. assessment_evidence와 동일 enum. API 응답은 소문자로 직렬화. RAG가 role을 분류한 경우에만 값을 채우며 nullable.']
}

Table user_favorites {
user_id bigint [pk, not null]
policy_id bigint [pk, not null]
saved_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
}

Table compare_basket {
compare_id bigint [pk, increment]
user_id bigint [not null]
policy_id bigint [not null]
created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]

indexes {
(user_id, policy_id) [unique]
}
}

Table user_policy_progress {
progress_id bigint [pk, increment]
user_id bigint [not null]
policy_id bigint [not null]

progress_status varchar(50) [not null, default: 'NOT_STARTED', note: '신청 대행 기능 도입 시 활용 예정. 현재 미사용(POST 시 PREPARING 고정). 값: NOT_STARTED, PREPARING, APPLIED, UNDER_REVIEW, APPROVED, REJECTED, RECEIVED']
progress_percent integer [not null, default: 0, note: '0~100, checklist completion cached value']
memo text

started_at timestamp
applied_at timestamp
decided_at timestamp
received_at timestamp

created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]

indexes {
(user_id, policy_id) [unique]
}
}

Table policy_checklist_template {
template_item_id bigint [pk, increment]
policy_id bigint [not null]

item_code varchar(100) [not null, note: 'unique within each policy']
item_label varchar(255) [not null]
item_description text
is_required boolean [not null, default: true]
display_order integer [not null, default: 0]
is_active boolean [not null, default: true]

created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]

indexes {
(policy_id, item_code) [unique]
}
}

Table user_policy_checklist_item {
user_checklist_item_id bigint [pk, increment]
progress_id bigint [not null]
template_item_id bigint [not null]

item_status varchar(30) [not null, default: 'PENDING', note: 'PENDING, DONE']
checked_at timestamp
note text

created_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
updated_at timestamp [not null, default: `CURRENT_TIMESTAMP`]

indexes {
(progress_id, template_item_id) [unique]
}
}
Table compare_history {
compare_history_id bigint [pk, increment]
user_id bigint [not null]
title varchar(255)
compared_at timestamp [not null, default: `CURRENT_TIMESTAMP`]
deleted_at timestamp

indexes {
(user_id, compared_at)
}
}

Table compare_history_item {
compare_history_item_id bigint [pk, increment]
compare_history_id bigint [not null]
policy_id bigint [not null]
added_at timestamp [not null, default: `CURRENT_TIMESTAMP`]

indexes {
(compare_history_id, policy_id) [unique]
}
}

Ref: compare_history.user_id > users.user_id [delete: cascade]
Ref: compare_history_item.compare_history_id > compare_history.compare_history_id [delete: cascade]
Ref: compare_history_item.policy_id > policy.policy_id [delete: cascade]

Ref: user_profile.user_id > users.user_id [delete: cascade]
Ref: family_member.user_id > users.user_id [delete: cascade]

Ref: policy_detail.policy_id > policy.policy_id [delete: cascade]
Ref: policy_tag.policy_id > policy.policy_id [delete: cascade]
Ref: policy_rule.policy_id > policy.policy_id [delete: cascade]
Ref: required_document.policy_id > policy.policy_id [delete: cascade]
Ref: policy_document.policy_id > policy.policy_id [delete: cascade]
Ref: policy_document_chunk.document_id > policy_document.document_id [delete: cascade]

Ref: chat_session.user_id > users.user_id [delete: cascade]
Ref: chat_message.chat_session_id > chat_session.chat_session_id [delete: cascade]
Ref: chat_message.parent_message_id > chat_message.chat_message_id [delete: set null]

Ref: recommendation_request.user_id > users.user_id [delete: cascade]

Ref: eligibility_request.user_id > users.user_id [delete: cascade]
Ref: eligibility_request.policy_id > policy.policy_id [delete: cascade]

Ref: follow_up_question.request_id > recommendation_request.request_id [delete: cascade]
Ref: follow_up_question.answer_message_id > chat_message.chat_message_id [delete: set null]

Ref: recommendation_candidate.request_id > recommendation_request.request_id [delete: cascade]
Ref: recommendation_candidate.policy_id > policy.policy_id [delete: cascade]

Ref: policy_assessment.request_id > recommendation_request.request_id [delete: cascade]
Ref: policy_assessment.recommendation_request_id > recommendation_request.request_id [delete: cascade]
Ref: policy_assessment.eligibility_request_id > eligibility_request.request_id [delete: cascade]
Ref: policy_assessment.policy_id > policy.policy_id [delete: cascade]

Ref: assessment_evidence.assessment_id > policy_assessment.assessment_id [delete: cascade]
Ref: assessment_evidence.chunk_id > policy_document_chunk.chunk_id [delete: cascade]

Ref: chat_message_policy.chat_message_id > chat_message.chat_message_id [delete: cascade]
Ref: chat_message_policy.policy_id > policy.policy_id [delete: cascade]

Ref: chat_message_evidence.chat_message_id > chat_message.chat_message_id [delete: cascade]
Ref: chat_message_evidence.chunk_id > policy_document_chunk.chunk_id [delete: cascade]

Ref: user_favorites.user_id > users.user_id [delete: cascade]
Ref: user_favorites.policy_id > policy.policy_id [delete: cascade]

Ref: compare_basket.user_id > users.user_id [delete: cascade]
Ref: compare_basket.policy_id > policy.policy_id [delete: cascade]

Ref: user_policy_progress.user_id > users.user_id [delete: cascade]
Ref: user_policy_progress.policy_id > policy.policy_id [delete: cascade]
Ref: policy_checklist_template.policy_id > policy.policy_id [delete: cascade]
Ref: user_policy_checklist_item.progress_id > user_policy_progress.progress_id [delete: cascade]
Ref: user_policy_checklist_item.template_item_id > policy_checklist_template.template_item_id [delete: cascade]
