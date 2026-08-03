#!/bin/bash
# Bootstrap wrapper.
#
# On a fresh clone the CLI is not on PATH yet, so this is the one command you
# can run straight from the repository. All the logic lives in Python; once
# setup has finished, use `odoo-runbot-local` (or `orl`) instead.
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bin/odoo-runbot-local" setup "$@"
