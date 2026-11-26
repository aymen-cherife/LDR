use blake2::{
    digest::{Update, VariableOutput},
    Blake2bVar,
};
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple,
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

#[pyfunction]
fn encode(py: Python<'_>, walk: Bound<'_, PyAny>, value: Py<PyAny>) -> PyResult<String> {
    let hash = Py::new(py, Hash::new())?;
    let mut stack = vec![Task::Node(value, hash.clone_ref(py))];
    let active = walk.getattr("active")?.downcast_into::<PyDict>()?;
    let ceiling: Option<usize> = walk.getattr("budget")?.getattr("max_nodes")?.extract()?;
    let fingerprint = py.import("ldr.fingerprint")?;
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
                let scalar = value.is_none()
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
                if value.is_none() {
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
                    if (list || tuple) && value.len()? >= 32 {
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
                            let stats = fingerprint.getattr("STATS")?;
                            let count: usize = stats.getattr("python_nodes")?.extract()?;
                            stats.setattr("python_nodes", count + value.len()?)?;
                            walk.call_method1("_fast_items", (value, out.bind(py)))?;
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
