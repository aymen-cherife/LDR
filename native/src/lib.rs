use blake2::{
    digest::{Update, VariableOutput},
    Blake2bVar,
};
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString,
    PyTuple,
};

#[pyclass]
struct Hash {
    state: Blake2bVar,
}

impl Hash {
    fn new() -> Self {
        Self {
            state: Blake2bVar::new(16).unwrap(),
        }
    }
    fn bytes(&self) -> Vec<u8> {
        let mut result = vec![0; 16];
        self.state.clone().finalize_variable(&mut result).unwrap();
        result
    }
}
#[pymethods]
impl Hash {
    fn update(&mut self, data: &[u8]) {
        self.state.update(data);
    }
    fn digest<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.bytes())
    }
    fn hexdigest(&self) -> String {
        self.bytes().iter().map(|b| format!("{b:02x}")).collect()
    }
}

enum Task {
    Node(Py<PyAny>, Py<Hash>),
    Children(Py<PyAny>),
    Items(Py<PyAny>, Py<Hash>),
    DictItems(Py<PyAny>, Py<Hash>),
    SetItems(Py<PyAny>, Py<Hash>, Vec<Py<Hash>>, bool),
    Close(usize, Py<Hash>),
}

fn write(py: Python<'_>, hash: &Py<Hash>, data: &[u8]) {
    hash.borrow_mut(py).state.update(data);
}

fn secret_text(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    fingerprint: &Bound<'_, PyModule>,
) -> PyResult<()> {
    let redact = py.import("ldr.redact")?;
    let cache = fingerprint
        .getattr("_secret_cache")?
        .downcast_into::<PyDict>()?;
    let short = value.len()? <= fingerprint.getattr("SHORT_TEXT")?.extract::<usize>()?;
    let cached = if short { cache.get_item(value)? } else { None };
    let secret = if let Some(found) = cached {
        found.is_truthy()?
    } else {
        let mut found = false;
        for pair in redact.getattr("_environment")?.get_item(0)?.try_iter()? {
            if value.contains(pair?.get_item(0)?)? {
                found = true;
                break;
            }
        }
        if !found {
            for item in redact.getattr("_argv_secrets")?.get_item(1)?.try_iter()? {
                let item = item?;
                if item.len()? > 0
                    && (value.eq(&item)?
                        || (item.len()? >= redact.getattr("_ENV_MIN_LENGTH")?.extract()?
                            && value.contains(&item)?))
                {
                    found = true;
                    break;
                }
            }
        }
        if !found {
            let lower = value.call_method0("lower")?;
            let markers = redact.getattr("_MARKERS")?;
            for shape in redact.getattr("_SHAPES")?.try_iter()? {
                let shape = shape?;
                let mut eligible = false;
                for marker in markers.get_item(shape.get_item(0)?)?.try_iter()? {
                    if lower.contains(marker?)? {
                        eligible = true;
                        break;
                    }
                }
                if eligible
                    && !shape
                        .get_item(1)?
                        .call_method1("search", (value,))?
                        .is_none()
                {
                    found = true;
                    break;
                }
            }
        }
        if short {
            if cache.len() >= fingerprint.getattr("CACHE_LIMIT")?.extract()? {
                cache.clear();
            }
            cache.set_item(value, found)?;
        }
        found
    };
    if secret {
        return Err(PyErr::from_value(
            fingerprint.getattr("_Stop")?.call1(("redacted",))?,
        ));
    }
    Ok(())
}
fn text_bytes(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    fingerprint: &Bound<'_, PyModule>,
) -> PyResult<Vec<u8>> {
    secret_text(py, value, fingerprint)?;
    let regex = fingerprint.getattr("_TMP_RE")?;
    let normalized = if regex.is_none() {
        value.clone()
    } else {
        let pair = regex.call_method1("subn", (fingerprint.getattr("_TMP_PLACEHOLDER")?, value))?;
        let count: usize = pair.get_item(1)?.extract()?;
        let stats = fingerprint.getattr("STATS")?;
        let old: usize = stats.getattr("tmp_normalized")?.extract()?;
        stats.setattr("tmp_normalized", old + count)?;
        pair.get_item(0)?
    };
    let data = normalized.call_method1("encode", ("utf-8", "surrogatepass"))?;
    let bytes = data.downcast::<PyBytes>()?.as_bytes();
    let mut encoded = format!("s{}:", bytes.len()).into_bytes();
    encoded.extend_from_slice(bytes);
    Ok(encoded)
}

#[pyfunction]
fn encode(py: Python<'_>, walk: Bound<'_, PyAny>, value: Py<PyAny>) -> PyResult<String> {
    let hash = Py::new(py, Hash::new())?;
    let mut stack = vec![Task::Node(value, hash.clone_ref(py))];
    let active = walk.getattr("active")?.downcast_into::<PyDict>()?;
    let ceiling: Option<usize> = walk.getattr("budget")?.getattr("max_nodes")?.extract()?;
    let fingerprint = py.import("ldr.fingerprint")?;
    fingerprint.getattr("_current_caches")?.call0()?;
    let secret = py.import("ldr.redact")?.getattr("is_secret_name")?;
    while let Some(task) = stack.pop() {
        match task {
            Task::Close(marker, out) => {
                write(py, &out, b"]");
                active.del_item(marker)?;
            }
            Task::Children(iterator) => {
                if let Some(child) = iterator.bind(py).try_iter()?.next() {
                    let child = child?;
                    let value = child.get_item(0)?.unbind();
                    let out = child.get_item(1)?.extract::<Py<Hash>>()?;
                    stack.push(Task::Children(iterator));
                    stack.push(Task::Node(value, out));
                }
            }
            Task::Items(iterator, out) => {
                if let Some(child) = iterator.bind(py).try_iter()?.next() {
                    let child = child?.unbind();
                    stack.push(Task::Items(iterator, out.clone_ref(py)));
                    stack.push(Task::Node(child, out));
                }
            }
            Task::DictItems(iterator, out) => {
                if let Some(pair) = iterator.bind(py).try_iter()?.next() {
                    let pair = pair?;
                    let key = pair.get_item(0)?;
                    if secret.call1((&key,))?.is_truthy()? {
                        return Err(PyErr::from_value(
                            fingerprint.getattr("_Stop")?.call1(("redacted",))?,
                        ));
                    }
                    let value = pair.get_item(1)?.unbind();
                    stack.push(Task::DictItems(iterator, out.clone_ref(py)));
                    stack.push(Task::Node(value, out.clone_ref(py)));
                    stack.push(Task::Node(key.unbind(), out));
                }
            }
            Task::SetItems(iterator, out, mut parts, frozen) => {
                if let Some(child) = iterator.bind(py).try_iter()?.next() {
                    let child = child?.unbind();
                    let part = Py::new(py, Hash::new())?;
                    parts.push(part.clone_ref(py));
                    stack.push(Task::SetItems(iterator, out, parts, frozen));
                    stack.push(Task::Node(child, part));
                } else {
                    let mut bytes: Vec<_> = parts.iter().map(|p| p.borrow(py).bytes()).collect();
                    bytes.sort();
                    write(
                        py,
                        &out,
                        format!("{}{}(", if frozen { "z" } else { "e" }, bytes.len()).as_bytes(),
                    );
                    for part in bytes {
                        write(py, &out, &part);
                    }
                }
            }
            Task::Node(value, out) => {
                let value = value.bind(py);
                let kind = value.get_type();
                let list = kind.is(&py.get_type::<PyList>());
                let tuple = kind.is(&py.get_type::<PyTuple>());
                let dict = kind.is(&py.get_type::<PyDict>());
                let set = kind.is(&py.get_type::<PySet>());
                let frozen = kind.is(&py.get_type::<PyFrozenSet>());
                let text = kind.is(&py.get_type::<PyString>());
                let bytes =
                    kind.is(&py.get_type::<PyBytes>()) || kind.is(&py.get_type::<PyByteArray>());
                let scalar = bytes
                    || text
                    || value.is_none()
                    || kind.is(&py.get_type::<PyBool>())
                    || kind.is(&py.get_type::<PyInt>())
                    || kind.is(&py.get_type::<PyFloat>());
                if !(scalar || list || tuple || dict || set || frozen) {
                    let count: usize = fingerprint
                        .getattr("STATS")?
                        .getattr("python_nodes")?
                        .extract()?;
                    fingerprint
                        .getattr("STATS")?
                        .setattr("python_nodes", count + 1)?;
                    let children = walk.call_method1("_encode", (value, out.bind(py)))?;
                    if !children.is_none() {
                        stack.push(Task::Children(children.unbind()));
                    }
                    continue;
                }
                let visited: usize = walk.getattr("visited")?.extract()?;
                walk.setattr("visited", visited + 1)?;
                if ceiling.is_some_and(|limit| visited + 1 > limit) {
                    return Err(PyErr::from_value(
                        fingerprint.getattr("_Stop")?.call1(("unknown",))?,
                    ));
                }
                if bytes {
                    let decoded = value.call_method1("decode", ("latin-1",))?;
                    secret_text(py, &decoded, &fingerprint)?;
                    let data = py.get_type::<PyBytes>().call1((value,))?;
                    let data = data.downcast::<PyBytes>()?.as_bytes();
                    let mutable = kind.is(&py.get_type::<PyByteArray>());
                    if mutable {
                        walk.setattr("immutable", false)?;
                    }
                    write(
                        py,
                        &out,
                        format!("{}{}:", if mutable { "B" } else { "b" }, data.len()).as_bytes(),
                    );
                    write(py, &out, data);
                } else if text {
                    write(py, &out, &text_bytes(py, value, &fingerprint)?);
                } else if value.is_none() {
                    write(py, &out, b"N");
                } else if kind.is(&py.get_type::<PyBool>()) {
                    write(py, &out, if value.is_truthy()? { b"T" } else { b"F" });
                } else if kind.is(&py.get_type::<PyInt>()) {
                    let hex = py
                        .import("builtins")?
                        .getattr("format")?
                        .call1((value, "x"))?
                        .extract::<String>()?;
                    write(py, &out, format!("i{hex};").as_bytes());
                } else if kind.is(&py.get_type::<PyFloat>()) {
                    let hex: String = value.call_method0("hex")?.extract()?;
                    write(py, &out, format!("f{hex};").as_bytes());
                } else {
                    if list || dict || set {
                        walk.setattr("immutable", false)?;
                    }
                    let marker = value.as_ptr() as usize;
                    if let Some(depth) = active.get_item(marker)? {
                        let depth: usize = depth.extract()?;
                        write(py, &out, format!("@{};", active.len() - depth).as_bytes());
                        continue;
                    }
                    active.set_item(marker, active.len())?;
                    stack.push(Task::Close(marker, out.clone_ref(py)));
                    // Python's string bulk path checks redaction before its budget check.
                    // Keep that exact ordering, including normalization counters.
                    if (list || tuple)
                        && value.len()?
                            >= fingerprint.getattr("FAST_PATH_MIN")?.extract::<usize>()?
                    {
                        let strings = value.try_iter()?.all(|item| {
                            item.map(|v| v.get_type().is(&py.get_type::<PyString>()))
                                .unwrap_or(false)
                        });
                        if strings {
                            write(
                                py,
                                &out,
                                format!("{}{}[", if list { "l" } else { "t" }, value.len()?)
                                    .as_bytes(),
                            );
                            let mut body = Vec::new();
                            for item in value.try_iter()? {
                                body.extend(text_bytes(py, &item?, &fingerprint)?);
                            }
                            let visited: usize = walk.getattr("visited")?.extract()?;
                            let visited = visited + value.len()?;
                            walk.setattr("visited", visited)?;
                            if ceiling.is_some_and(|limit| visited > limit) {
                                return Err(PyErr::from_value(
                                    fingerprint.getattr("_Stop")?.call1(("unknown",))?,
                                ));
                            }
                            write(py, &out, &body);
                            continue;
                        }
                    }
                    if set || frozen {
                        stack.push(Task::SetItems(
                            value.try_iter()?.into_any().unbind(),
                            out,
                            Vec::new(),
                            frozen,
                        ));
                    } else {
                        let tag = if list {
                            "l"
                        } else if tuple {
                            "t"
                        } else {
                            "d"
                        };
                        let open = if dict { "{" } else { "[" };
                        write(py, &out, format!("{tag}{}{open}", value.len()?).as_bytes());
                        if dict {
                            stack.push(Task::DictItems(
                                value.call_method0("items")?.try_iter()?.into_any().unbind(),
                                out,
                            ));
                        } else {
                            stack.push(Task::Items(value.try_iter()?.into_any().unbind(), out));
                        }
                    }
                }
            }
        }
    }
    let result = hash.borrow(py).hexdigest();
    Ok(result)
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Hash>()?;
    m.add_function(wrap_pyfunction!(encode, m)?)?;
    Ok(())
}
