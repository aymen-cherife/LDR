# Native signature prototype

Build with the checkout's Python and copy the extension into `ldr/` for local use.

```sh
. .venv/bin/activate
maturin build --release --manifest-path native/Cargo.toml -o /tmp/ldr-wheels
python - <<'PY'
import zipfile
from pathlib import Path
with zipfile.ZipFile(max(Path('/tmp/ldr-wheels').glob('*.whl'), key=lambda path: path.stat().st_mtime)) as wheel:
    for name in wheel.namelist():
        if Path(name).name.startswith('_native.') and name.endswith(('.so', '.pyd')):
            Path('ldr', Path(name).name).write_bytes(wheel.read(name))
PY
```

Importability selects the reader. Remove the generated extension to use Python.
For a controlled comparison, provide two checkouts with the extension present only
in the native checkout to `evaluation/final_check.py --python-reader PATH --native-reader PATH`.
The phase JSON includes `python_reader_nodes`, the count of nodes delegated to Python.
The stored developer status also includes this count under `native_reader.python_nodes`.
The prototype uses Python for text policies, object layouts and other fallback nodes.
Rust and maturin are needed only to build the extension.
