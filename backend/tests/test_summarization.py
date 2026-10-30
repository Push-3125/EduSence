"""Verification suite alias for Document Summarization Service (TASK-M5-01 / US 2.14).

Imports all comprehensive tests from test_document_summaries.
"""

import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

try:
    from test_document_summaries import (
        client,
        fake_chunk_repo,
        fake_doc_repo,
        fake_gemini,
        fake_job_repo,
        fake_logger,
        fake_quota,
        fake_summary_repo,
        fake_user_repo,
        setup_environment,
        test_async_summary_job_creation_and_polling,
        test_celery_generate_summary_task_execution,
        test_empty_document_handling,
        test_quota_and_circuit_breaker_errors,
        test_strict_tenant_isolation,
        test_summaries_crud_and_delete,
        test_summary_types_validation,
        test_sync_summary_generation_happy_path,
    )
except ImportError:
    from tests.test_document_summaries import (
        client,
        fake_chunk_repo,
        fake_doc_repo,
        fake_gemini,
        fake_job_repo,
        fake_logger,
        fake_quota,
        fake_summary_repo,
        fake_user_repo,
        setup_environment,
        test_async_summary_job_creation_and_polling,
        test_celery_generate_summary_task_execution,
        test_empty_document_handling,
        test_quota_and_circuit_breaker_errors,
        test_strict_tenant_isolation,
        test_summaries_crud_and_delete,
        test_summary_types_validation,
        test_sync_summary_generation_happy_path,
    )

__all__ = [
    "client",
    "fake_chunk_repo",
    "fake_doc_repo",
    "fake_gemini",
    "fake_job_repo",
    "fake_logger",
    "fake_quota",
    "fake_summary_repo",
    "fake_user_repo",
    "setup_environment",
    "test_async_summary_job_creation_and_polling",
    "test_celery_generate_summary_task_execution",
    "test_empty_document_handling",
    "test_quota_and_circuit_breaker_errors",
    "test_strict_tenant_isolation",
    "test_summaries_crud_and_delete",
    "test_summary_types_validation",
    "test_sync_summary_generation_happy_path",
]
