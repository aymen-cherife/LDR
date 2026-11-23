"""``python -m ldr``: same as the ``ldr`` command, for when ldr.exe cannot run."""

import sys

from .launch import main

sys.exit(main())
