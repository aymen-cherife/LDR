use pyo3::sync::GILOnceCell;
static REFERENCE_VALUES: GILOnceCell<Py<PyDict>> = GILOnceCell::new();
struct References {
    modules: HashMap<&'static str, Py<PyModule>>,
    values: HashMap<&'static str, Py<PyAny>>,
}
impl References {
    fn resolve(py: Python<'_>) -> PyResult<Self> {
        let mut result = Self {
            modules: HashMap::new(),
            values: HashMap::new(),
        };
        result.modules.insert("abc", py.import("abc")?.unbind());
        result
            .modules
            .insert("builtins", py.import("builtins")?.unbind());
        result
            .modules
            .insert("datetime", py.import("datetime")?.unbind());
        result
            .modules
            .insert("decimal", py.import("decimal")?.unbind());
        result
            .modules
            .insert("functools", py.import("functools")?.unbind());
        result
            .modules
            .insert("ldr.fingerprint", py.import("ldr.fingerprint")?.unbind());
        result
            .modules
            .insert("ldr.redact", py.import("ldr.redact")?.unbind());
        result
            .modules
            .insert("ldr.structure", py.import("ldr.structure")?.unbind());
        result.modules.insert("types", py.import("types")?.unbind());
        result.values.insert(
            "abc:ABCMeta",
            result.module(py, "abc").getattr("ABCMeta")?.unbind(),
        );
        result.values.insert(
            "builtins:format",
            result.module(py, "builtins").getattr("format")?.unbind(),
        );
        result.values.insert(
            "builtins:range",
            result.module(py, "builtins").getattr("range")?.unbind(),
        );
        result.values.insert(
            "builtins:sorted",
            result.module(py, "builtins").getattr("sorted")?.unbind(),
        );
        result.values.insert(
            "datetime:timedelta",
            result.module(py, "datetime").getattr("timedelta")?.unbind(),
        );
        result.values.insert(
            "datetime:timezone",
            result.module(py, "datetime").getattr("timezone")?.unbind(),
        );
        result.values.insert(
            "decimal:Decimal",
            result.module(py, "decimal").getattr("Decimal")?.unbind(),
        );
        result.values.insert(
            "functools:partial",
            result.module(py, "functools").getattr("partial")?.unbind(),
        );
        result.values.insert(
            "ldr.fingerprint:CACHE_LIMIT",
            result
                .module(py, "ldr.fingerprint")
                .getattr("CACHE_LIMIT")?
                .unbind(),
        );
        result.values.insert(
            "ldr.fingerprint:FAST_PATH_MIN",
            result
                .module(py, "ldr.fingerprint")
                .getattr("FAST_PATH_MIN")?
                .unbind(),
        );
        result.values.insert(
            "ldr.fingerprint:SHORT_TEXT",
            result
                .module(py, "ldr.fingerprint")
                .getattr("SHORT_TEXT")?
                .unbind(),
        );
        result.values.insert(
            "ldr.fingerprint:STATS",
            result
                .module(py, "ldr.fingerprint")
                .getattr("STATS")?
                .unbind(),
        );
        result.values.insert(
            "ldr.fingerprint:_Stop",
            result
                .module(py, "ldr.fingerprint")
                .getattr("_Stop")?
                .unbind(),
        );
        result.values.insert(
            "ldr.fingerprint:_TMP_PLACEHOLDER",
            result
                .module(py, "ldr.fingerprint")
                .getattr("_TMP_PLACEHOLDER")?
                .unbind(),
        );
        result.values.insert(
            "ldr.fingerprint:_TMP_RE",
            result
                .module(py, "ldr.fingerprint")
                .getattr("_TMP_RE")?
                .unbind(),
        );
        result.values.insert(
            "ldr.fingerprint:_current_caches",
            result
                .module(py, "ldr.fingerprint")
                .getattr("_current_caches")?
                .unbind(),
        );
        result.values.insert(
            "ldr.fingerprint:_secret_cache",
            result
                .module(py, "ldr.fingerprint")
                .getattr("_secret_cache")?
                .unbind(),
        );
        result.values.insert(
            "ldr.redact:_ENV_MIN_LENGTH",
            result
                .module(py, "ldr.redact")
                .getattr("_ENV_MIN_LENGTH")?
                .unbind(),
        );
        result.values.insert(
            "ldr.redact:_MARKERS",
            result
                .module(py, "ldr.redact")
                .getattr("_MARKERS")?
                .unbind(),
        );
        result.values.insert(
            "ldr.redact:_SHAPES",
            result.module(py, "ldr.redact").getattr("_SHAPES")?.unbind(),
        );
        result.values.insert(
            "ldr.redact:_argv_secrets",
            result
                .module(py, "ldr.redact")
                .getattr("_argv_secrets")?
                .unbind(),
        );
        result.values.insert(
            "ldr.redact:_environment",
            result
                .module(py, "ldr.redact")
                .getattr("_environment")?
                .unbind(),
        );
        result.values.insert(
            "ldr.redact:is_secret_name",
            result
                .module(py, "ldr.redact")
                .getattr("is_secret_name")?
                .unbind(),
        );
        result.values.insert(
            "ldr.structure:_BASE_EXC_ARGS",
            result
                .module(py, "ldr.structure")
                .getattr("_BASE_EXC_ARGS")?
                .unbind(),
        );
        result.values.insert(
            "ldr.structure:_TYPE_MODULE",
            result
                .module(py, "ldr.structure")
                .getattr("_TYPE_MODULE")?
                .unbind(),
        );
        result.values.insert(
            "ldr.structure:_TYPE_NAME",
            result
                .module(py, "ldr.structure")
                .getattr("_TYPE_NAME")?
                .unbind(),
        );
        result.values.insert(
            "ldr.structure:_TYPE_QUALNAME",
            result
                .module(py, "ldr.structure")
                .getattr("_TYPE_QUALNAME")?
                .unbind(),
        );
        result.values.insert(
            "ldr.structure:_layout",
            result
                .module(py, "ldr.structure")
                .getattr("_layout")?
                .unbind(),
        );
        result.values.insert(
            "types:BuiltinFunctionType",
            result
                .module(py, "types")
                .getattr("BuiltinFunctionType")?
                .unbind(),
        );
        result.values.insert(
            "types:FunctionType",
            result.module(py, "types").getattr("FunctionType")?.unbind(),
        );
        result.values.insert(
            "types:MemberDescriptorType",
            result
                .module(py, "types")
                .getattr("MemberDescriptorType")?
                .unbind(),
        );
        result.values.insert(
            "types:MethodType",
            result.module(py, "types").getattr("MethodType")?.unbind(),
        );
        for (key, name) in [
            ("ldr.redact:_name_cache", "_name_cache"),
            ("ldr.redact:_CAMEL", "_CAMEL"),
            ("ldr.redact:_SECRET_NAME", "_SECRET_NAME"),
        ] {
            result
                .values
                .insert(key, result.module(py, "ldr.redact").getattr(name)?.unbind());
        }
        let saved =
            REFERENCE_VALUES.get_or_try_init(py, || Ok::<_, PyErr>(PyDict::new(py).unbind()))?;
        let saved = saved.bind(py);
        for (name, value) in result.values.iter_mut() {
            if let Some(previous) = saved.get_item(*name)? {
                if previous.is(value.bind(py)) {
                    *value = previous.unbind();
                    continue;
                }
            }
            saved.set_item(*name, value.bind(py))?;
        }
        Ok(result)
    }
    fn module<'py>(&self, py: Python<'py>, key: &str) -> Bound<'py, PyModule> {
        self.modules[key].bind(py).clone()
    }
    fn get<'py>(&self, py: Python<'py>, key: &str) -> Bound<'py, PyAny> {
        self.values[key].bind(py).clone()
    }
}
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

fn secret_name(
    py: Python<'_>,
    name: &Bound<'_, PyAny>,
    refs: &References,
    plans: &mut HashMap<usize, Option<ObjectPlan>>,
) -> PyResult<bool> {
    if !name.is_exact_instance_of::<PyString>() {
        if name.is_instance_of::<PyString>() {
            plans.clear();
            return refs
                .get(py, "ldr.redact:is_secret_name")
                .call1((name,))?
                .is_truthy();
        }
        return Ok(false);
    }
    let cache = refs
        .get(py, "ldr.redact:_name_cache")
        .downcast_into::<PyDict>()?;
    if let Some(found) = cache.get_item(name)? {
        return found.is_truthy();
    }
    let expanded = refs
        .get(py, "ldr.redact:_CAMEL")
        .call_method1("sub", ("_", name))?;
    let found = !refs
        .get(py, "ldr.redact:_SECRET_NAME")
        .call_method1("search", (expanded,))?
        .is_none();
    if cache.len() >= 65_536 {
        cache.clear();
    }
    cache.set_item(name, found)?;
    Ok(found)
}

fn secret_text(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    fingerprint: &Bound<'_, PyModule>,
    refs: &References,
) -> PyResult<()> {
    let redact = refs.module(py, "ldr.redact");
    let cache = refs
        .get(py, "ldr.fingerprint:_secret_cache")
        .downcast_into::<PyDict>()?;
    let short = value.len()?
        <= refs
            .get(py, "ldr.fingerprint:SHORT_TEXT")
            .extract::<usize>()?;
    let cached = if short { cache.get_item(value)? } else { None };
    let secret = if let Some(found) = cached {
        found.is_truthy()?
    } else {
        let mut found = false;
        for pair in refs
            .get(py, "ldr.redact:_environment")
            .get_item(0)?
            .try_iter()?
        {
            if value.contains(pair?.get_item(0)?)? {
                found = true;
                break;
            }
        }
        if !found {
            for item in refs
                .get(py, "ldr.redact:_argv_secrets")
                .get_item(1)?
                .try_iter()?
            {
                let item = item?;
                if item.len()? > 0
                    && (value.eq(&item)?
                        || (item.len()? >= refs.get(py, "ldr.redact:_ENV_MIN_LENGTH").extract()?
                            && value.contains(&item)?))
                {
                    found = true;
                    break;
                }
            }
        }
        if !found {
            let lower = value.call_method0("lower")?;
            let markers = refs.get(py, "ldr.redact:_MARKERS");
            for shape in refs.get(py, "ldr.redact:_SHAPES").try_iter()? {
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
            if cache.len() >= refs.get(py, "ldr.fingerprint:CACHE_LIMIT").extract()? {
                cache.clear();
            }
            cache.set_item(value, found)?;
        }
        found
    };
    if secret {
        return Err(PyErr::from_value(
            refs.get(py, "ldr.fingerprint:_Stop").call1(("redacted",))?,
        ));
    }
    Ok(())
}
fn text_bytes(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    fingerprint: &Bound<'_, PyModule>,
    refs: &References,
) -> PyResult<Vec<u8>> {
    secret_text(py, value, fingerprint, refs)?;
    let regex = refs.get(py, "ldr.fingerprint:_TMP_RE");
    let normalized = if regex.is_none() {
        value.clone()
    } else {
        let pair = regex.call_method1(
            "subn",
            (refs.get(py, "ldr.fingerprint:_TMP_PLACEHOLDER"), value),
        )?;
        let count: usize = pair.get_item(1)?.extract()?;
        let stats = refs.get(py, "ldr.fingerprint:STATS");
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

fn identity(py: Python<'_>, cls: &Bound<'_, PyAny>, refs: &References) -> PyResult<Vec<u8>> {
    let structure = refs.module(py, "ldr.structure");
    let module = refs.get(py, "ldr.structure:_TYPE_MODULE").call1((cls,))?;
    let name = refs.get(py, "ldr.structure:_TYPE_QUALNAME").call1((cls,))?;
    let module = if module.is_exact_instance_of::<PyString>() {
        module
    } else {
        PyString::new(py, "").into_any()
    };
    let name = if name.is_exact_instance_of::<PyString>() {
        name
    } else {
        refs.get(py, "ldr.structure:_TYPE_NAME").call1((cls,))?
    };
    let text = PyString::new(py, ".").call_method1("join", ((module, name),))?;
    Ok(text
        .call_method1("encode", ("utf-8", "surrogatepass"))?
        .downcast::<PyBytes>()?
        .as_bytes()
        .to_vec())
}
struct ObjectPlan {
    identity: Vec<u8>,
    base: Option<Py<PyAny>>,
    named: Option<Py<PyAny>>,
    exception: bool,
    members: Vec<(Py<PyAny>, Py<PyAny>)>,
    dictionary: Option<Py<PyAny>>,
    has_state: bool,
}
fn object_fields(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    plans: &mut HashMap<usize, Option<ObjectPlan>>,
    refs: &References,
) -> PyResult<Option<(Vec<u8>, Vec<(Option<Py<PyAny>>, Py<PyAny>)>)>> {
    let cls = value.get_type();
    let marker = cls.as_ptr() as usize;
    if !plans.contains_key(&marker) {
        let structure = refs.module(py, "ldr.structure");
        // The existing weak per-class reader performs full mutation validation.
        let layout = refs.get(py, "ldr.structure:_layout").call1((&cls,))?;
        let reader = layout.get_item(0)?;
        let namespaces = layout.get_item(1)?;
        let types = refs.module(py, "types");
        for namespace in namespaces.try_iter()? {
            let namespace = namespace?;
            for hook in ["__getattr__", "__getattribute__"] {
                if namespace
                    .call_method1("get", (hook,))?
                    .is_instance_of::<pyo3::types::PyFunction>()
                {
                    plans.insert(marker, None);
                    return Ok(None);
                }
            }
        }
        let owners = reader.getattr("owners")?;
        let mut members = Vec::new();
        for index in (0..owners.len()?).rev() {
            let owner = owners.get_item(index)?;
            let namespace = namespaces.get_item(index)?;
            for member in owner.getattr("members")?.try_iter()? {
                let member = member?;
                let name = member.get_item(0)?;
                let descriptor = namespace.call_method1("get", (member.get_item(1)?,))?;
                if !descriptor
                    .get_type()
                    .is(&refs.get(py, "types:MemberDescriptorType"))
                {
                    plans.insert(marker, None);
                    return Ok(None);
                }
                members.push((name.unbind(), descriptor.unbind()));
            }
        }
        let dictionary = reader.getattr("dictionary")?;
        let dictionary = if dictionary.is_none() {
            None
        } else {
            Some(
                namespaces
                    .get_item(dictionary.extract::<usize>()?)?
                    .get_item("__dict__")?
                    .unbind(),
            )
        };
        let base = reader.getattr("base")?;
        let named = reader.getattr("named_fields")?;
        let identity = reader
            .getattr("identity")?
            .call_method1("encode", ("utf-8", "surrogatepass"))?
            .downcast::<PyBytes>()?
            .as_bytes()
            .to_vec();
        plans.insert(
            marker,
            Some(ObjectPlan {
                identity,
                base: if base.is_none() {
                    None
                } else {
                    Some(base.unbind())
                },
                named: if named.is_none() {
                    None
                } else {
                    Some(named.unbind())
                },
                exception: reader.getattr("exception")?.extract()?,
                members,
                dictionary,
                has_state: reader.getattr("has_state")?.extract()?,
            }),
        );
    }
    let plan = match plans.get(&marker).unwrap() {
        Some(plan) => plan,
        None => return Ok(None),
    };
    let mut fields = Vec::new();
    if let Some(base) = &plan.base {
        let base = base.bind(py);
        let iter = if base.is(&py.get_type::<PyDict>()) {
            base.getattr("items")?.call1((value,))?
        } else {
            base.getattr("__iter__")?.call1((value,))?
        };
        let items = iter.try_iter()?.collect::<PyResult<Vec<_>>>()?;
        let names = plan
            .named
            .as_ref()
            .filter(|n| n.bind(py).len().ok() == Some(items.len()));
        for (index, item) in items.into_iter().enumerate() {
            fields.push((
                match names {
                    Some(names) => Some(names.bind(py).get_item(index)?.unbind()),
                    None => None,
                },
                item.unbind(),
            ));
        }
    }
    if plan.exception {
        fields.push((
            Some(PyString::new(py, "args").into_any().unbind()),
            refs.get(py, "ldr.structure:_BASE_EXC_ARGS")
                .call1((value,))?
                .unbind(),
        ));
    }
    for (name, descriptor) in &plan.members {
        match descriptor.bind(py).call_method1("__get__", (value,)) {
            Ok(item) => fields.push((Some(name.clone_ref(py)), item.unbind())),
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => (),
            Err(error) => return Err(error),
        }
    }
    let mut state = false;
    if let Some(descriptor) = &plan.dictionary {
        if let Ok(dictionary) = descriptor.bind(py).call_method1("__get__", (value,)) {
            if let Ok(dictionary) = dictionary.downcast::<PyDict>() {
                state = true;
                for (name, item) in dictionary.iter() {
                    if name.is_exact_instance_of::<PyString>() {
                        fields.push((Some(name.unbind()), item.unbind()));
                    }
                }
            }
        }
    }
    if fields.is_empty() && !state && !plan.has_state {
        return Ok(None);
    }
    Ok(Some((plan.identity.clone(), fields)))
}

fn tagged(tag: &str, bytes: &[u8]) -> Vec<u8> {
    let mut result = format!("{tag}{}:", bytes.len()).into_bytes();
    result.extend(bytes);
    result
}
fn leaf_bytes(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    refs: &References,
) -> PyResult<Option<Vec<u8>>> {
    let kind = value.get_type();
    let types = refs.module(py, "types");
    if kind.is(&py.get_type::<pyo3::types::PyType>()) || kind.is(&refs.get(py, "abc:ABCMeta")) {
        return Ok(Some(tagged("C", &identity(py, value, refs)?)));
    }
    if kind.is(&refs.get(py, "types:FunctionType"))
        || kind.is(&refs.get(py, "types:BuiltinFunctionType"))
    {
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
    if kind.is(&refs.get(py, "builtins:range")) {
        let start = value.getattr("start")?.str()?.to_str()?.to_owned();
        let stop = value.getattr("stop")?.str()?.to_str()?.to_owned();
        let step = value.getattr("step")?.str()?.to_str()?.to_owned();
        return Ok(Some(tagged(
            "R",
            format!("{start},{stop},{step}").as_bytes(),
        )));
    }
    let datetime = refs.module(py, "datetime");
    for name in ["datetime", "date", "time"] {
        if kind.is(&datetime.getattr(name)?) {
            if name != "date" {
                let zone = value.getattr("tzinfo")?;
                if !zone.is_none() && !zone.get_type().is(&refs.get(py, "datetime:timezone")) {
                    return Ok(None);
                }
            }
            let text: String = value.call_method0("isoformat")?.extract()?;
            let label: String = refs
                .get(py, "ldr.structure:_TYPE_NAME")
                .call1((&kind,))?
                .extract()?;
            return Ok(Some(tagged("M", format!("{label}{text}").as_bytes())));
        }
    }
    if kind.is(&refs.get(py, "datetime:timedelta")) {
        let days: i64 = value.getattr("days")?.extract()?;
        let seconds: i64 = value.getattr("seconds")?.extract()?;
        let micros: i64 = value.getattr("microseconds")?.extract()?;
        return Ok(Some(tagged(
            "M",
            format!("td{days},{seconds},{micros}").as_bytes(),
        )));
    }
    if kind.is(&refs.get(py, "decimal:Decimal")) {
        return Ok(Some(tagged("D", value.str()?.to_str()?.as_bytes())));
    }
    Ok(None)
}

#[pyfunction]
fn encode(py: Python<'_>, walk: Bound<'_, PyAny>, value: Py<PyAny>) -> PyResult<String> {
    let mut refs = References::resolve(py)?;
    let hash = Hash::new().state;
    let mut stack = vec![Task::Node(value, hash.clone())];
    let mut active: HashMap<usize, usize> = walk.getattr("active")?.extract()?;
    let initial_active = active.clone();
    let mut plans = HashMap::new();
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
    let fingerprint = refs.module(py, "ldr.fingerprint");
    refs.get(py, "ldr.fingerprint:_current_caches").call0()?;
    let secret = refs.get(py, "ldr.redact:is_secret_name");
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
                            if secret_name(py, name.bind(py), &refs, &mut plans)? {
                                return Err(PyErr::from_value(
                                    refs.get(py, "ldr.fingerprint:_Stop").call1(("redacted",))?,
                                ));
                            }
                            let data = name
                                .bind(py)
                                .call_method1("encode", ("utf-8", "surrogatepass"))?;
                            let bytes = data.downcast::<PyBytes>()?.as_bytes();
                            write(py, &out, &tagged("n", bytes));
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
                    plans.clear();
                    to_python!();
                    let next = iterator.bind(py).try_iter()?.next();
                    from_python!();
                    refs = References::resolve(py)?;
                    refs.get(py, "ldr.fingerprint:_current_caches").call0()?;
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
                        if secret_name(py, &key, &refs, &mut plans)? {
                            return Err(PyErr::from_value(
                                refs.get(py, "ldr.fingerprint:_Stop").call1(("redacted",))?,
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
                        let mut body =
                            format!("{}{}(", if frozen { "z" } else { "e" }, bytes.len())
                                .into_bytes();
                        for part in bytes {
                            body.extend(part);
                        }
                        write(py, &out, &body);
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
                        let method = kind.is(&refs.get(py, "types:MethodType"));
                        let partial = kind.is(&refs.get(py, "functools:partial"));
                        if method || partial {
                            visited += 1;
                            if ceiling.is_some_and(|limit| visited > limit) {
                                return Err(PyErr::from_value(
                                    refs.get(py, "ldr.fingerprint:_Stop").call1(("unknown",))?,
                                ));
                            }
                            immutable = false;
                            if method {
                                write(py, &out, b"Mt");
                                let receiver = value.getattr("__self__")?;
                                stack.push(Task::Raw(
                                    tagged(
                                        "T",
                                        &identity(py, receiver.get_type().as_any(), &refs)?,
                                    ),
                                    out.clone(),
                                ));
                                stack.push(Task::Node(value.getattr("__func__")?.unbind(), out));
                            } else {
                                write(py, &out, b"Pa");
                                let kwargs =
                                    value.getattr("keywords")?.downcast_into::<PyDict>()?;
                                let keys = refs.get(py, "builtins:sorted").call1((&kwargs,))?;
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
                        if let Some(bytes) = leaf_bytes(py, value, &refs)? {
                            visited += 1;
                            if ceiling.is_some_and(|limit| visited > limit) {
                                return Err(PyErr::from_value(
                                    refs.get(py, "ldr.fingerprint:_Stop").call1(("unknown",))?,
                                ));
                            }
                            if !kind.is(&py.get_type::<pyo3::types::PyComplex>()) {
                                immutable = false;
                            }
                            write(py, &out, &bytes);
                            continue;
                        }
                        // Known leaves retain their distinct tags. Other safe layouts are read here.
                        let types = refs.module(py, "types");
                        let known = kind.is(&py.get_type::<pyo3::types::PyType>())
                            || kind.is(&refs.get(py, "abc:ABCMeta"))
                            || kind.is(&refs.get(py, "types:FunctionType"))
                            || kind.is(&refs.get(py, "types:BuiltinFunctionType"));
                        let known = known
                            || kind.is(&refs.get(py, "functools:partial"))
                            || kind.is(&refs.get(py, "types:MethodType"))
                            || kind.is(&refs.get(py, "decimal:Decimal"))
                            || ["datetime", "date", "time", "timedelta"]
                                .iter()
                                .any(|name| {
                                    kind.is(&refs.module(py, "datetime").getattr(*name).unwrap())
                                })
                            || kind.is(&py.get_type::<pyo3::types::PyComplex>())
                            || kind.is(&refs.get(py, "builtins:range"))
                            || value.is(&py.Ellipsis())
                            || value.is(&py.NotImplemented());
                        if !known {
                            if let Ok(Some((identity, fields))) =
                                object_fields(py, value, &mut plans, &refs)
                            {
                                visited += 1;
                                if ceiling.is_some_and(|limit| visited > limit) {
                                    return Err(PyErr::from_value(
                                        refs.get(py, "ldr.fingerprint:_Stop")
                                            .call1(("unknown",))?,
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
                                let mut header = tagged("O", &identity);
                                header.extend(format!("{}{{", fields.len()).as_bytes());
                                write(py, &out, &header);
                                stack.push(Task::ObjectClose(marker, out.clone()));
                                stack.push(Task::Fields(fields.into_iter(), out));
                                continue;
                            }
                        }
                        let count: usize = refs
                            .get(py, "ldr.fingerprint:STATS")
                            .getattr("python_nodes")?
                            .extract()?;
                        refs.get(py, "ldr.fingerprint:STATS")
                            .setattr("python_nodes", count + 1)?;
                        plans.clear();
                        to_python!();
                        let wrapped = Py::new(py, Hash { state: out.clone() })?;
                        let children = walk.call_method1("_encode", (value, wrapped));
                        from_python!();
                        refs = References::resolve(py)?;
                        refs.get(py, "ldr.fingerprint:_current_caches").call0()?;
                        refs = References::resolve(py)?;
                        refs.get(py, "ldr.fingerprint:_current_caches").call0()?;
                        let children = children?;
                        if !children.is_none() {
                            stack.push(Task::Children(children.unbind()));
                        }
                        continue;
                    }
                    visited += 1;
                    if ceiling.is_some_and(|limit| visited > limit) {
                        return Err(PyErr::from_value(
                            refs.get(py, "ldr.fingerprint:_Stop").call1(("unknown",))?,
                        ));
                    }
                    if bytes {
                        let decoded = value.call_method1("decode", ("latin-1",))?;
                        secret_text(py, &decoded, &fingerprint, &refs)?;
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
                        write(py, &out, &text_bytes(py, value, &fingerprint, &refs)?);
                    } else if value.is_none() {
                        write(py, &out, b"N");
                    } else if kind.is(&py.get_type::<PyBool>()) {
                        write(py, &out, if value.is_truthy()? { b"T" } else { b"F" });
                    } else if kind.is(&py.get_type::<PyInt>()) {
                        let hex = refs
                            .get(py, "builtins:format")
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
                            && value.len()? > 0
                            && value.len()?
                                >= refs
                                    .get(py, "ldr.fingerprint:FAST_PATH_MIN")
                                    .extract::<usize>()?
                        {
                            let first = value.get_item(0)?;
                            let item_type = first.get_type();
                            let eligible = item_type.is(&py.get_type::<PyString>())
                                || item_type.is(&py.get_type::<PyInt>())
                                || item_type.is(&py.get_type::<PyFloat>())
                                || item_type.is(&py.get_type::<PyBool>());
                            let mut homogeneous = eligible;
                            for index in 0..value.len()? {
                                if !value.get_item(index)?.get_type().is(&item_type) {
                                    homogeneous = false;
                                    break;
                                }
                            }
                            if homogeneous {
                                write(
                                    py,
                                    &out,
                                    format!("{}{}[", if list { "l" } else { "t" }, value.len()?)
                                        .as_bytes(),
                                );
                                let mut body = Vec::new();
                                for index in 0..value.len()? {
                                    let item = value.get_item(index)?;
                                    if item_type.is(&py.get_type::<PyString>()) {
                                        body.extend(text_bytes(py, &item, &fingerprint, &refs)?);
                                    } else if item_type.is(&py.get_type::<PyBool>()) {
                                        body.extend(if item.is_truthy()? { b"T" } else { b"F" });
                                    } else if item_type.is(&py.get_type::<PyFloat>()) {
                                        let hex: String = item.call_method0("hex")?.extract()?;
                                        body.extend(format!("f{hex};").as_bytes());
                                    } else {
                                        let hex: String = refs
                                            .get(py, "builtins:format")
                                            .call1((&item, "x"))?
                                            .extract()?;
                                        body.extend(format!("i{hex};").as_bytes());
                                    }
                                }
                                visited += value.len()?;
                                if ceiling.is_some_and(|limit| visited > limit) {
                                    return Err(PyErr::from_value(
                                        refs.get(py, "ldr.fingerprint:_Stop")
                                            .call1(("unknown",))?,
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
                                    value.clone().unbind(),
                                    0,
                                    value.len()?,
                                    value.len()?,
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
