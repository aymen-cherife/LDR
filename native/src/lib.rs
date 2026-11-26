use blake2::{
    digest::{Update, VariableOutput},
    Blake2bVar,
};
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString,
    PyTuple,
};
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
type Digest = Rc<RefCell<Blake2bVar>>;

#[pyclass(unsendable)]
struct Hash {
    state: Digest,
}

impl Hash {
    fn new() -> Self {
        Self {
            state: Rc::new(RefCell::new(Blake2bVar::new(16).unwrap())),
        }
    }
    fn bytes(&self) -> Vec<u8> {
        let mut result = vec![0; 16];
        self.state
            .borrow()
            .clone()
            .finalize_variable(&mut result)
            .unwrap();
        result
    }
}
#[pymethods]
impl Hash {
    fn update(&mut self, data: &[u8]) {
        self.state.borrow_mut().update(data);
    }
    fn digest<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.bytes())
    }
    fn hexdigest(&self) -> String {
        self.bytes().iter().map(|b| format!("{b:02x}")).collect()
    }
}

enum Task {
    Node(Py<PyAny>, Digest),
    Children(Py<PyAny>),
    Items(Py<PyAny>, usize, Digest),
    DictItems(Py<PyAny>, isize, usize, usize, Digest),
    SetItems(Py<PyAny>, Digest, Vec<Digest>, bool),
    Close(usize, Digest),
    Raw(Vec<u8>, Digest),
    ObjectClose(usize, Digest),
    Fields(std::vec::IntoIter<(Option<Py<PyAny>>, Py<PyAny>)>, Digest),
}

fn write(_py: Python<'_>, hash: &Digest, data: &[u8]) {
    hash.borrow_mut().update(data);
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
    let mut exception = false;
    let mut namespaces = Vec::new();
    let mut dictionary = None;
    let mut base = None;
    let mut named = None;
    let mut has_state = mro.len()? == 1;
    for owner in mro.try_iter()? {
        let owner = owner?;
        if owner.is(&py.get_type::<pyo3::exceptions::PyBaseException>()) {
            exception = true;
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
        let named = if base.is(&py.get_type::<PyTuple>()) {
            named
        } else {
            None
        };
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
    if exception {
        fields.push((
            Some(PyString::new(py, "args").into_any().unbind()),
            structure
                .getattr("_BASE_EXC_ARGS")?
                .call1((value,))?
                .unbind(),
        ));
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

fn tagged(tag: &str, bytes: &[u8]) -> Vec<u8> {
    let mut result = format!("{tag}{}:", bytes.len()).into_bytes();
    result.extend(bytes);
    result
}
fn leaf_bytes(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Option<Vec<u8>>> {
    let kind = value.get_type();
    let types = py.import("types")?;
    if kind.is(&py.get_type::<pyo3::types::PyType>())
        || kind.is(&py.import("abc")?.getattr("ABCMeta")?)
    {
        return Ok(Some(tagged("C", &identity(py, value)?)));
    }
    if kind.is(&types.getattr("FunctionType")?) || kind.is(&types.getattr("BuiltinFunctionType")?) {
        let text = PyString::new(py, ".").call_method1(
            "join",
            ((
                {
                    let module = value.getattr("__module__")?;
                    if module.is_none() {
                        PyString::new(py, "None").into_any()
                    } else if module.is_exact_instance_of::<PyString>() {
                        module
                    } else {
                        return Ok(None);
                    }
                },
                value.getattr("__qualname__")?,
            ),),
        )?;
        let bytes = text.call_method1("encode", ("utf-8", "surrogatepass"))?;
        return Ok(Some(tagged("Y", bytes.downcast::<PyBytes>()?.as_bytes())));
    }
    if kind.is(&py.get_type::<pyo3::types::PyComplex>()) {
        let real: String = value.getattr("real")?.call_method0("hex")?.extract()?;
        let imag: String = value.getattr("imag")?.call_method0("hex")?.extract()?;
        return Ok(Some(format!("c{real},{imag};").into_bytes()));
    }
    if value.is(&py.Ellipsis()) {
        return Ok(Some(b"E".to_vec()));
    }
    if value.is(&py.NotImplemented()) {
        return Ok(Some(b"X".to_vec()));
    }
    if kind.is(&py.import("builtins")?.getattr("range")?) {
        let start = value.getattr("start")?.str()?.to_str()?.to_owned();
        let stop = value.getattr("stop")?.str()?.to_str()?.to_owned();
        let step = value.getattr("step")?.str()?.to_str()?.to_owned();
        return Ok(Some(tagged(
            "R",
            format!("{start},{stop},{step}").as_bytes(),
        )));
    }
    let datetime = py.import("datetime")?;
    for name in ["datetime", "date", "time"] {
        if kind.is(&datetime.getattr(name)?) {
            if name != "date" {
                let zone = value.getattr("tzinfo")?;
                if !zone.is_none() && !zone.get_type().is(&datetime.getattr("timezone")?) {
                    return Ok(None);
                }
            }
            let text: String = value.call_method0("isoformat")?.extract()?;
            let label: String = py
                .import("ldr.structure")?
                .getattr("_TYPE_NAME")?
                .call1((&kind,))?
                .extract()?;
            return Ok(Some(tagged("M", format!("{label}{text}").as_bytes())));
        }
    }
    if kind.is(&datetime.getattr("timedelta")?) {
        let days: i64 = value.getattr("days")?.extract()?;
        let seconds: i64 = value.getattr("seconds")?.extract()?;
        let micros: i64 = value.getattr("microseconds")?.extract()?;
        return Ok(Some(tagged(
            "M",
            format!("td{days},{seconds},{micros}").as_bytes(),
        )));
    }
    if kind.is(&py.import("decimal")?.getattr("Decimal")?) {
        return Ok(Some(tagged("D", value.str()?.to_str()?.as_bytes())));
    }
    Ok(None)
}

#[pyfunction]
fn encode(py: Python<'_>, walk: Bound<'_, PyAny>, value: Py<PyAny>) -> PyResult<String> {
    let hash = Hash::new().state;
    let mut stack = vec![Task::Node(value, hash.clone())];
    let mut active: HashMap<usize, usize> = walk.getattr("active")?.extract()?;
    let initial_active = active.clone();
    let mut visited: usize = walk.getattr("visited")?.extract()?;
    let mut immutable: bool = walk.getattr("immutable")?.extract()?;
    let mut partial: bool = walk.getattr("partial")?.extract()?;
    macro_rules! to_python {
        () => {{
            walk.setattr("visited", visited)?;
            walk.setattr("immutable", immutable)?;
            walk.setattr("partial", partial)?;
            let dict = walk.getattr("active")?.downcast_into::<PyDict>()?;
            dict.clear();
            for (key, value) in &active {
                dict.set_item(*key, *value)?;
            }
        }};
    }
    macro_rules! from_python {
        () => {{
            visited = walk.getattr("visited")?.extract()?;
            immutable = walk.getattr("immutable")?.extract()?;
            partial = walk.getattr("partial")?.extract()?;
            active = walk.getattr("active")?.extract()?;
        }};
    }

    let ceiling: Option<usize> = walk.getattr("budget")?.getattr("max_nodes")?.extract()?;
    let fingerprint = py.import("ldr.fingerprint")?;
    fingerprint.getattr("_current_caches")?.call0()?;
    let secret = py.import("ldr.redact")?.getattr("is_secret_name")?;
    let outcome: PyResult<()> = (|| {
        while let Some(task) = stack.pop() {
            match task {
                Task::Raw(bytes, out) => write(py, &out, &bytes),
                Task::ObjectClose(marker, out) => {
                    write(py, &out, b"}");
                    active.remove(&marker);
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
                        stack.push(Task::Fields(fields, out.clone()));
                        stack.push(Task::Node(value, out));
                    }
                }
                Task::Close(marker, out) => {
                    write(py, &out, b"]");
                    active.remove(&marker);
                }
                Task::Children(iterator) => {
                    to_python!();
                    let next = iterator.bind(py).try_iter()?.next();
                    from_python!();
                    if let Some(child) = next {
                        let child = child?;
                        let value = child.get_item(0)?.unbind();
                        let out = child.get_item(1)?.extract::<PyRef<Hash>>()?.state.clone();
                        stack.push(Task::Children(iterator));
                        stack.push(Task::Node(value, out));
                    }
                }
                Task::Items(container, index, out) => {
                    let bound = container.bind(py);
                    if index < bound.len()? {
                        let child = if let Ok(list) = bound.downcast::<PyList>() {
                            list.get_item(index)?
                        } else {
                            bound.downcast::<PyTuple>()?.get_item(index)?
                        };
                        stack.push(Task::Items(container.clone_ref(py), index + 1, out.clone()));
                        stack.push(Task::Node(child.unbind(), out));
                    }
                }
                Task::DictItems(container, mut position, expected, remaining, out) => {
                    let dictionary = container.bind(py).downcast::<PyDict>()?;
                    if dictionary.len() != expected {
                        return Err(pyo3::exceptions::PyRuntimeError::new_err(
                            "dictionary changed size during iteration",
                        ));
                    }
                    let mut key = std::ptr::null_mut();
                    let mut value = std::ptr::null_mut();
                    let present = unsafe {
                        pyo3::ffi::PyDict_Next(
                            dictionary.as_ptr(),
                            &mut position,
                            &mut key,
                            &mut value,
                        )
                    };
                    if present != 0 {
                        if remaining == 0 {
                            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                                "dictionary keys changed during iteration",
                            ));
                        }
                        let key = unsafe { Bound::from_borrowed_ptr(py, key) };
                        let value = unsafe { Bound::from_borrowed_ptr(py, value) };
                        if secret.call1((&key,))?.is_truthy()? {
                            return Err(PyErr::from_value(
                                fingerprint.getattr("_Stop")?.call1(("redacted",))?,
                            ));
                        }
                        stack.push(Task::DictItems(
                            container.clone_ref(py),
                            position,
                            expected,
                            remaining - 1,
                            out.clone(),
                        ));
                        stack.push(Task::Node(value.unbind(), out.clone()));
                        stack.push(Task::Node(key.unbind(), out));
                    }
                }
                Task::SetItems(iterator, out, mut parts, frozen) => {
                    if let Some(child) = iterator.bind(py).try_iter()?.next() {
                        let child = child?.unbind();
                        let part = Hash::new().state;
                        parts.push(part.clone());
                        stack.push(Task::SetItems(iterator, out, parts, frozen));
                        stack.push(Task::Node(child, part));
                    } else {
                        let mut bytes: Vec<_> = parts
                            .iter()
                            .map(|p| {
                                let mut b = vec![0; 16];
                                p.borrow().clone().finalize_variable(&mut b).unwrap();
                                b
                            })
                            .collect();
                        bytes.sort();
                        write(
                            py,
                            &out,
                            format!("{}{}(", if frozen { "z" } else { "e" }, bytes.len())
                                .as_bytes(),
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
                    let bytes = kind.is(&py.get_type::<PyBytes>())
                        || kind.is(&py.get_type::<PyByteArray>());
                    let scalar = bytes
                        || text
                        || value.is_none()
                        || kind.is(&py.get_type::<PyBool>())
                        || kind.is(&py.get_type::<PyInt>())
                        || kind.is(&py.get_type::<PyFloat>());
                    if !(scalar || list || tuple || dict || set || frozen) {
                        let method = kind.is(&py.import("types")?.getattr("MethodType")?);
                        let partial = kind.is(&py.import("functools")?.getattr("partial")?);
                        if method || partial {
                            visited += 1;
                            if ceiling.is_some_and(|limit| visited > limit) {
                                return Err(PyErr::from_value(
                                    fingerprint.getattr("_Stop")?.call1(("unknown",))?,
                                ));
                            }
                            immutable = false;
                            if method {
                                write(py, &out, b"Mt");
                                let receiver = value.getattr("__self__")?;
                                stack.push(Task::Raw(
                                    tagged("T", &identity(py, receiver.get_type().as_any())?),
                                    out.clone(),
                                ));
                                stack.push(Task::Node(value.getattr("__func__")?.unbind(), out));
                            } else {
                                write(py, &out, b"Pa");
                                let kwargs =
                                    value.getattr("keywords")?.downcast_into::<PyDict>()?;
                                let keys = py
                                    .import("builtins")?
                                    .getattr("sorted")?
                                    .call1((&kwargs,))?;
                                let mut fields = Vec::new();
                                for key in keys.try_iter()? {
                                    let key = key?;
                                    fields.push((
                                        Some(key.clone().unbind()),
                                        kwargs.get_item(&key)?.unwrap().unbind(),
                                    ));
                                }
                                stack.push(Task::Raw(b"}".to_vec(), out.clone()));
                                stack.push(Task::Fields(fields.into_iter(), out.clone()));
                                stack.push(Task::Raw(
                                    format!("{}{{", kwargs.len()).into_bytes(),
                                    out.clone(),
                                ));
                                stack
                                    .push(Task::Node(value.getattr("args")?.unbind(), out.clone()));
                                stack.push(Task::Node(value.getattr("func")?.unbind(), out));
                            }
                            continue;
                        }
                        if let Some(bytes) = leaf_bytes(py, value)? {
                            visited += 1;
                            if ceiling.is_some_and(|limit| visited > limit) {
                                return Err(PyErr::from_value(
                                    fingerprint.getattr("_Stop")?.call1(("unknown",))?,
                                ));
                            }
                            if !kind.is(&py.get_type::<pyo3::types::PyComplex>()) {
                                immutable = false;
                            }
                            write(py, &out, &bytes);
                            continue;
                        }
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
                            || value.is(&py.Ellipsis())
                            || value.is(&py.NotImplemented());
                        if !known {
                            if let Ok(Some((identity, fields))) = object_fields(py, value) {
                                visited += 1;
                                if ceiling.is_some_and(|limit| visited > limit) {
                                    return Err(PyErr::from_value(
                                        fingerprint.getattr("_Stop")?.call1(("unknown",))?,
                                    ));
                                }
                                immutable = false;
                                let marker = value.as_ptr() as usize;
                                if let Some(&depth) = active.get(&marker) {
                                    write(
                                        py,
                                        &out,
                                        format!("@{};", active.len() - depth).as_bytes(),
                                    );
                                    continue;
                                }
                                active.insert(marker, active.len());
                                write(py, &out, format!("O{}:", identity.len()).as_bytes());
                                write(py, &out, &identity);
                                write(py, &out, format!("{}{{", fields.len()).as_bytes());
                                stack.push(Task::ObjectClose(marker, out.clone()));
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
                        to_python!();
                        let wrapped = Py::new(py, Hash { state: out.clone() })?;
                        let children = walk.call_method1("_encode", (value, wrapped));
                        from_python!();
                        let children = children?;
                        if !children.is_none() {
                            stack.push(Task::Children(children.unbind()));
                        }
                        continue;
                    }
                    visited += 1;
                    if ceiling.is_some_and(|limit| visited > limit) {
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
                            immutable = false;
                        }
                        write(
                            py,
                            &out,
                            format!("{}{}:", if mutable { "B" } else { "b" }, data.len())
                                .as_bytes(),
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
                            immutable = false;
                        }
                        let marker = value.as_ptr() as usize;
                        if let Some(&depth) = active.get(&marker) {
                            write(py, &out, format!("@{};", active.len() - depth).as_bytes());
                            continue;
                        }
                        active.insert(marker, active.len());
                        stack.push(Task::Close(marker, out.clone()));
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
                                visited += value.len()?;
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
                                    value.clone().unbind(), 0, value.len()?, value.len()?,
                                    out,
                                ));
                            } else {
                                stack.push(Task::Items(value.clone().unbind(), 0, out));
                            }
                        }
                    }
                }
            }
        }
        Ok(())
    })();
    if outcome.is_err() {
        active = initial_active;
    }
    to_python!();
    outcome?;
    let result = Hash { state: hash }.hexdigest();
    Ok(result)
}

#[pyfunction]
fn capture(py: Python<'_>, generator: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let mut sent = py.None();
    let mut error: Option<PyErr> = None;
    loop {
        let next = match error.take() {
            Some(failure) => generator.call_method1("throw", (failure.value(py),)),
            None => generator.call_method1("send", (sent.bind(py),)),
        };
        let item = match next {
            Ok(item) => item,
            Err(failure) if failure.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
                return Ok(failure.value(py).getattr("value")?.unbind());
            }
            Err(failure) => return Err(failure),
        };
        let walk = item.get_item(0)?;
        let value = item.get_item(1)?.unbind();
        match encode(py, walk, value) {
            Ok(digest) => sent = PyString::new(py, &digest).into_any().unbind(),
            Err(failure) => error = Some(failure),
        }
    }
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Hash>()?;
    m.add_function(wrap_pyfunction!(encode, m)?)?;
    m.add_function(wrap_pyfunction!(capture, m)?)?;
    Ok(())
}
