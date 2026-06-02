#!/usr/bin/env bash
# merge_train: pre-commit hook (domain locking removed).
#
# Domain locking was removed in favor of pure symbol-level conflict
# prediction via predict-conflicts. This hook is now a no-op.
# See: chore/remove-domain-locking PR.
exit 0
