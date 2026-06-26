# Tasks

- [x] Read `WHILLY_GITLAB_SSH_HOST` in `whilly/scheduler/intake.py` clone-URL
      derivation; neutral default `gitlab.example.com`.
- [x] Read `WHILLY_GITLAB_SSH_HOST` in `whilly/sinks/gitlab_mr.py`
      remote-host inference fallback; neutral default `gitlab.example.com`.
- [x] Update `tests/test_scheduler_intake.py`: assert neutral default and the
      env override.
- [x] Apply + archive this change; `make spec-check` green.
