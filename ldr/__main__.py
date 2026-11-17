"""``python -m ldr``: same as the ``ldr`` command, for when ldr.exe cannot run."""

import sys

from .cli import main

sys.exit(main())
