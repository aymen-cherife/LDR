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
    ObjectClose(usize, Py<Hash>),
    Fields(std::vec::IntoIter<(Option<Py<PyAny>>, Py<PyAny>)>, Py<Hash>),
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

fn identity(py: Python<'_>, cls: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let structure = py.import("ldr.structure")?;
    let module = structure.getattr("_TYPE_MODULE")?.call1((cls,))?;
    let name = structure.getattr("_TYPE_QUALNAME")?.call1((cls,))?;
    let module = if module.is_exact_instance_of::<PyString>() {
        module
    } else {
        PyString::new(py, "").into_any()
    };
    let name = if name.is_exact_instance_of::<PyString>() {
        name
    } else {
        structure.getattr("_TYPE_NAME")?.call1((cls,))?
    };
    let text = PyString::new(py, ".").call_method1("join", ((module, name),))?;
    Ok(text
        .call_method1("encode", ("utf-8", "surrogatepass"))?
        .downcast::<PyBytes>()?
        .as_bytes()
        .to_vec())
}
fn object_fields(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
) -> PyResult<Option<(Vec<u8>, Vec<(Option<Py<PyAny>>, Py<PyAny>)>)>> {
    let structure = py.import("ldr.structure")?;
    let cls = value.get_type();
    let mro = structure.getattr("_TYPE_MRO")?.call1((&cls,))?;
    let types = py.import("types")?;
    let member_type = types.getattr("MemberDescriptorType")?;
    let getset_type = types.getattr("GetSetDescriptorType")?;
    let mut namespaces = Vec::new();
    let mut dictionary = None;
    let mut base = None;
    let mut named = None;
    let mut has_state = mro.len()? == 1;
    for owner in mro.try_iter()? {
        let owner = owner?;
        if owner.is(&py.get_type::<pyo3::exceptions::PyBaseException>()) {
            return Ok(None);
        }
        let namespace = structure.getattr("_TYPE_DICT")?.call1((&owner,))?;
        for hook in ["__getattr__", "__getattribute__"] {
            let descriptor = namespace.call_method1("get", (hook,))?;
            if descriptor.is_instance_of::<pyo3::types::PyFunction>() {
                return Ok(None);
            }
        }
        for candidate in [
            py.get_type::<PyList>().into_any(),
            py.get_type::<PyTuple>().into_any(),
            py.get_type::<PyDict>().into_any(),
            py.get_type::<PySet>().into_any(),
            py.get_type::<PyFrozenSet>().into_any(),
        ] {
            if owner.is(&candidate) {
                base = Some(candidate);
            }
        }
        let fields = namespace.call_method1("get", ("_fields",))?;
        if named.is_none()
            && fields.is_exact_instance_of::<PyTuple>()
            && fields.try_iter()?.all(|v| {
                v.map(|v| v.is_exact_instance_of::<PyString>())
                    .unwrap_or(false)
            })
        {
            named = Some(fields);
        }
        let descriptor = namespace.call_method1("get", ("__dict__",))?;
        if dictionary.is_none() && descriptor.get_type().is(&getset_type) {
            dictionary = Some(descriptor);
            has_state = true;
        }
        if namespace.contains("__slots__")? {
            has_state = true;
        }
        namespaces.push((owner, namespace));
    }
    let mut fields = Vec::new();
    if let Some(base) = base {
        let iter = if base.is(&py.get_type::<PyDict>()) {
            base.getattr("items")?.call1((value,))?
        } else {
            base.getattr("__iter__")?.call1((value,))?
        };
        let items: Vec<_> = iter.try_iter()?.collect::<PyResult<Vec<_>>>()?;
        let names = if let Some(names) = named {
            if names.len()? == items.len() {
                Some(names)
            } else {
                None
            }
        } else {
            None
        };
        for (index, item) in items.into_iter().enumerate() {
            fields.push((
                if let Some(ref names) = names {
                    Some(names.get_item(index)?.unbind())
                } else {
                    None
                },
                item.unbind(),
            ));
        }
    }
    for (owner, namespace) in namespaces.iter().rev() {
        let slots = namespace.call_method1("get", ("__slots__",))?;
        let slots: Vec<_> = if slots.is_exact_instance_of::<PyString>() {
            vec![slots]
        } else if slots.is_exact_instance_of::<PyTuple>() || slots.is_exact_instance_of::<PyList>()
        {
            slots.try_iter()?.collect::<PyResult<Vec<_>>>()?
        } else {
            Vec::new()
        };
        for name in slots {
            if !name.is_exact_instance_of::<PyString>() {
                continue;
            }
            let text: String = name.extract()?;
            if text == "__dict__" || text == "__weakref__" {
                continue;
            }
            let stored = if text.starts_with("__") && !text.ends_with("__") {
                let owner_name: String = structure
                    .getattr("_TYPE_NAME")?
                    .call1((owner,))?
                    .extract()?;
                format!("_{}{text}", owner_name.trim_start_matches('_'))
            } else {
                text
            };
            let descriptor = namespace.call_method1("get", (stored,))?;
            if !descriptor.get_type().is(&member_type) {
                return Ok(None);
            }
            match descriptor.call_method1("__get__", (value,)) {
                Ok(item) => fields.push((Some(name.unbind()), item.unbind())),
                Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => (),
                Err(_) => return Ok(None),
            }
        }
    }
    let mut state = false;
    if let Some(descriptor) = dictionary {
        if let Ok(dict) = descriptor.call_method1("__get__", (value,)) {
            if let Ok(dict) = dict.downcast::<PyDict>() {
                state = true;
                for (key, item) in dict.iter() {
                    if key.is_exact_instance_of::<PyString>() {
                        fields.push((Some(key.unbind()), item.unbind()));
                    }
                }
            }
        }
    }
    if fields.is_empty() && !state && !has_state {
        return Ok(None);
    }
    Ok(Some((identity(py, cls.as_any())?, fields)))
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
            Task::ObjectClose(marker, out) => {
                write(py, &out, b"}");
                active.del_item(marker)?;
            }
            Task::Fields(mut fields, out) => {
                if let Some((name, value)) = fields.next() {
                    if let Some(name) = name {
                        if secret.call1((name.bind(py),))?.is_truthy()? {
                            return Err(PyErr::from_value(
                                fingerprint.getattr("_Stop")?.call1(("redacted",))?,
                            ));
                        }
                        let data = name
                            .bind(py)
                            .call_method1("encode", ("utf-8", "surrogatepass"))?;
                        let bytes = data.downcast::<PyBytes>()?.as_bytes();
                        write(py, &out, format!("n{}:", bytes.len()).as_bytes());
                        write(py, &out, bytes);
                    }
                    stack.push(Task::Fields(fields, out.clone_ref(py)));
                    stack.push(Task::Node(value, out));
                }
            }
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
                    // Known leaves retain their distinct tags. Other safe layouts are read here.
                    let types = py.import("types")?;
                    let known = kind.is(&py.get_type::<pyo3::types::PyType>())
                        || kind.is(&py.import("abc")?.getattr("ABCMeta")?)
                        || kind.is(&types.getattr("FunctionType")?)
                        || kind.is(&types.getattr("BuiltinFunctionType")?);
                    let known = known
                        || kind.is(&py.import("functools")?.getattr("partial")?)
                        || kind.is(&types.getattr("MethodType")?)
                        || kind.is(&py.import("decimal")?.getattr("Decimal")?)
                        || ["datetime", "date", "time", "timedelta"]
                            .iter()
                            .any(|name| {
                                kind.is(&py.import("datetime").unwrap().getattr(*name).unwrap())
                            })
                        || kind.is(&py.get_type::<pyo3::types::PyComplex>())
                        || kind.is(&py.import("builtins")?.getattr("range")?)
                        || value.is_ellipsis()
                        || value.is(&py.NotImplemented());
                    if !known {
                        if let Some((identity, fields)) = object_fields(py, value)? {
                            let visited: usize = walk.getattr("visited")?.extract()?;
                            walk.setattr("visited", visited + 1)?;
                            if ceiling.is_some_and(|limit| visited + 1 > limit) {
                                return Err(PyErr::from_value(
                                    fingerprint.getattr("_Stop")?.call1(("unknown",))?,
                                ));
                            }
                            walk.setattr("immutable", false)?;
                            let marker = value.as_ptr() as usize;
                            if let Some(depth) = active.get_item(marker)? {
                                let depth: usize = depth.extract()?;
                                write(py, &out, format!("@{};", active.len() - depth).as_bytes());
                                continue;
                            }
                            active.set_item(marker, active.len())?;
                            write(py, &out, format!("O{}:", identity.len()).as_bytes());
                            write(py, &out, &identity);
                            write(py, &out, format!("{}{{", fields.len()).as_bytes());
                            stack.push(Task::ObjectClose(marker, out.clone_ref(py)));
                            stack.push(Task::Fields(fields.into_iter(), out));
                            continue;
                        }
                    }
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
