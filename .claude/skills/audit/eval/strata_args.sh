# Sourced by strata_setup.sh and strata_check.sh: the commit pair, source roots
# and pytest arguments of strata's CI unit-test job, which the recording and
# every plan must share.
BASE=edc32ce0a13c5e1958bfcbfe93e1e2d36a31e19b
HEAD=1241020210ec29c2aebaea4390bae74b91772bfa
ROOTS=(--source-root src --source-root packages/strata-client/src
  --source-root packages/strata-pool/src --source-root tests=tests --source-root evals=evals)
PYTEST_ARGS=(tests/
  --ignore=tests/test_e2e_artifacts.py --ignore=tests/test_lineage.py
  --ignore=tests/test_s3_integration.py --ignore=tests/test_presigned_urls_minio.py
  --ignore=tests/test_presigned_urls_azurite.py --ignore=tests/test_lake_catalogs_integration.py
  --ignore=tests/test_artifact_store_postgres.py --ignore=tests/test_migrate_to_postgres.py
  --ignore=tests/notebook/test_e2e_mounts_s3.py --ignore=tests/notebook/test_e2e_mounts_azure.py
  --ignore=tests/notebook/test_e2e_mounts_gcs.py --ignore=tests/notebook/test_e2e_duckdb_lake.py
  -n auto --dist loadscope --tb=short)
