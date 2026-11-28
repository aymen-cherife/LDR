use crate::{layout, policy::Policy, unicode};
use blake2::{
    digest::{Update, VariableOutput},
    Blake2bVar,
};
use pyo3::{
    exceptions::PyValueError,
    ffi,
    prelude::*,
    types::{PyBytes, PyDict, PyTuple},
};
use rustc_hash::FxHashMap as HashMap;
use std::sync::Arc;
extern "C" {
    fn _PySet_NextEntry(
        set: *mut ffi::PyObject,
        pos: *mut isize,
        key: *mut *mut ffi::PyObject,
        hash: *mut isize,
    ) -> i32;
}
enum Task {
    Value(*mut ffi::PyObject, usize),
    Leave(usize),
    Sequence(*mut ffi::PyObject, isize, isize, bool, usize),
    Dict(*mut ffi::PyObject, isize, usize, isize, isize),
    Field(Option<Arc<Vec<u8>>>, *mut ffi::PyObject, usize),
    Resume(Py<PyAny>, usize),
    Set(*mut ffi::PyObject, isize, Vec<usize>, usize, bool, isize),
}
fn size(out: &mut Vec<u8>, n: usize) {
    out.extend_from_slice(&(n as u64).to_le_bytes())
}
fn text(out: &mut Vec<u8>, tag: u8, data: &[u8]) {
    out.push(tag);
    size(out, data.len());
    out.extend_from_slice(data)
}
fn digest(data: &[u8]) -> [u8; 16] {
    let mut hash = Blake2bVar::new(16).unwrap();
    hash.update(data);
    let mut out = [0; 16];
    hash.finalize_variable(&mut out).unwrap();
    out
}
#[pyclass(unsendable)]
pub struct ReaderCore {
    policy: Policy,
    fallback: Py<PyAny>,
    special: Vec<Py<PyAny>>,
    enum_meta: Py<PyAny>,
    dict_getter: usize,
    fast_min: usize,
    string_buffer: Vec<u8>,
    buffers: Vec<Vec<u8>>,
    names: HashMap<usize, (Py<PyAny>, Arc<Vec<u8>>)>,
    fields: Vec<(Option<Arc<Vec<u8>>>, *mut ffi::PyObject)>,
    dict_buffer: Vec<(*mut ffi::PyObject, *mut ffi::PyObject)>,
    tasks: Vec<Task>,
    active: HashMap<usize, usize>,
    secret_names: HashMap<usize, (Arc<Vec<u8>>, bool)>,
    delegations: HashMap<usize, (Py<PyAny>, usize)>,
}
#[pymethods]
impl ReaderCore {
    #[new]
    fn new(
        prefixes: Vec<String>,
        secrets: Vec<Vec<u8>>,
        arguments: Vec<(Vec<u8>, bool)>,
        patterns: Vec<String>,
        fallback: Py<PyAny>,
        special: Vec<Py<PyAny>>,
        enum_meta: Py<PyAny>,
        dict_getter: Py<PyAny>,
        fast_min: usize,
    ) -> PyResult<Self> {
        let getter = unsafe {
            if ffi::Py_TYPE(dict_getter.as_ptr()) != std::ptr::addr_of_mut!(ffi::PyGetSetDescr_Type)
            {
                return Err(PyValueError::new_err("invalid dictionary descriptor"));
            }
            let spec = (*(dict_getter.as_ptr() as *mut ffi::PyGetSetDescrObject)).d_getset;
            (*spec)
                .get
                .map(|f| f as *const () as usize)
                .ok_or_else(|| PyValueError::new_err("missing dictionary getter"))?
        };
        Ok(Self {
            policy: Policy::new(prefixes, secrets, arguments, patterns)
                .map_err(PyValueError::new_err)?,
            fallback,
            special,
            enum_meta,
            dict_getter: getter,
            fast_min,
            string_buffer: Vec::new(),
            buffers: Vec::new(),
            names: HashMap::default(),
            fields: Vec::new(),
            dict_buffer: Vec::new(),
            tasks: Vec::new(),
            active: HashMap::default(),
            secret_names: HashMap::default(),
            delegations: HashMap::default(),
        })
    }
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let d = PyDict::new(py);
        d.set_item("watchers", layout::watcher_count())?;
        Ok(d.unbind())
    }
    #[pyo3(signature = (value, partial_ok, max_nodes=None))]
    fn walk(
        &mut self,
        py: Python<'_>,
        value: &Bound<'_, PyAny>,
        partial_ok: bool,
        max_nodes: Option<usize>,
    ) -> PyResult<Py<PyDict>> {
        self.run(py, value, partial_ok, max_nodes, true)
    }
    #[pyo3(signature = (value, partial_ok, max_nodes=None))]
    fn capture(
        &mut self,
        py: Python<'_>,
        value: &Bound<'_, PyAny>,
        partial_ok: bool,
        max_nodes: Option<usize>,
    ) -> PyResult<Py<PyDict>> {
        self.run(py, value, partial_ok, max_nodes, false)
    }
}
impl ReaderCore {
    fn run(
        &mut self,
        py: Python<'_>,
        value: &Bound<'_, PyAny>,
        partial_ok: bool,
        max_nodes: Option<usize>,
        wire: bool,
    ) -> PyResult<Py<PyDict>> {
        self.delegations.clear();
        let mut tasks = std::mem::take(&mut self.tasks);
        tasks.push(Task::Value(value.as_ptr(), 0));
        let mut active = std::mem::take(&mut self.active);
        let mut visited = 0usize;
        let mut delegated = 0usize;
        let mut normalized = 0usize;
        let mut status = "complete".to_string();
        let mut partial = false;
        let mut immutable = true;
        for b in &mut self.buffers {
            b.clear()
        }
        if self.buffers.is_empty() {
            self.buffers.push(Vec::new())
        }
        let mut next_buffer = 1;
        let mut keepers = Vec::<Py<PyAny>>::new();
        let mut delegated_controls = HashMap::default();
        unsafe {
            while let Some(task) = tasks.pop() {
                let mut delegate = None;
                match task {
                    Task::Leave(address) => {
                        active.remove(&address);
                    }
                    Task::Field(name, item, target) => {
                        if let Some(ref name) = name {
                            if !item.is_null() {
                                let key = Arc::as_ptr(name) as usize;
                                let secret = if let Some((_, found)) = self.secret_names.get(&key) {
                                    *found
                                } else {
                                    let found = self.policy.secret_name(&name);
                                    self.secret_names.insert(key, (name.clone(), found));
                                    found
                                };
                                if secret {
                                    status = "redacted".into();
                                    break;
                                }
                            }
                            text(&mut self.buffers[target], b'n', name);
                        } else {
                            self.buffers[target].push(b'p')
                        }
                        if item.is_null() {
                            visited += 1;
                            if max_nodes.is_some_and(|limit| visited > limit) {
                                status = "unknown".into();
                                break;
                            }
                            self.buffers[target].push(b'u')
                        } else {
                            self.buffers[target].push(b'v');
                            tasks.push(Task::Value(item, target))
                        }
                    }
                    Task::Sequence(obj, index, count, tuple, target) => {
                        if index < (if tuple { count } else { ffi::PyList_Size(obj) }) {
                            tasks.push(Task::Sequence(obj, index + 1, count, tuple, target));
                            tasks.push(Task::Value(
                                if tuple {
                                    ffi::PyTuple_GetItem(obj, index)
                                } else {
                                    ffi::PyList_GetItem(obj, index)
                                },
                                target,
                            ))
                        }
                    }
                    Task::Dict(obj, mut position, target, expected, remaining) => {
                        if ffi::PyDict_Size(obj) != expected {
                            status = "failed".into();
                            break;
                        }
                        let mut key = std::ptr::null_mut();
                        let mut item = std::ptr::null_mut();
                        if ffi::PyDict_Next(obj, &mut position, &mut key, &mut item) != 0 {
                            if remaining == 0 {
                                status = "failed".into();
                                break;
                            }
                            if ffi::PyUnicode_Check(key) != 0 {
                                let k = Bound::from_borrowed_ptr(py, key);
                                if self.policy.secret_name(&unicode::payload_any(py, &k)?) {
                                    status = "redacted".into();
                                    break;
                                }
                            } else {
                                let k = ffi::Py_TYPE(key);
                                let simple = key == ffi::Py_None()
                                    || [
                                        std::ptr::addr_of_mut!(ffi::PyLong_Type),
                                        std::ptr::addr_of_mut!(ffi::PyBool_Type),
                                        std::ptr::addr_of_mut!(ffi::PyFloat_Type),
                                        std::ptr::addr_of_mut!(ffi::PyComplex_Type),
                                        std::ptr::addr_of_mut!(ffi::PyBytes_Type),
                                        std::ptr::addr_of_mut!(ffi::PyTuple_Type),
                                        std::ptr::addr_of_mut!(ffi::PyFrozenSet_Type),
                                        std::ptr::addr_of_mut!(ffi::PyType_Type),
                                    ]
                                    .contains(&k);
                                if !simple {
                                    if delegated_controls.insert(obj as usize, ()).is_none() {
                                        delegated += 1;
                                        self.record_delegation(py, ffi::Py_TYPE(obj), 1);
                                    }
                                    for address in active.keys() {
                                        keepers.push(Py::from_borrowed_ptr(
                                            py,
                                            *address as *mut ffi::PyObject,
                                        ));
                                    }
                                    for pending in &tasks {
                                        match pending {
                                            Task::Value(p, _) | Task::Field(_, p, _)
                                                if !p.is_null() =>
                                            {
                                                keepers.push(Py::from_borrowed_ptr(py, *p))
                                            }
                                            _ => {}
                                        }
                                    }
                                    keepers.push(Py::from_borrowed_ptr(py, key));
                                    keepers.push(Py::from_borrowed_ptr(py, item));
                                    match self.fallback.bind(py).call_method1(
                                        "secret_key",
                                        (Bound::from_borrowed_ptr(py, key),),
                                    ) {
                                        Ok(result) => {
                                            if result.extract::<bool>()? {
                                                status = "redacted".into();
                                                break;
                                            }
                                        }
                                        Err(_) => {
                                            status = "failed".into();
                                            break;
                                        }
                                    }
                                }
                            }
                            tasks.push(Task::Dict(obj, position, target, expected, remaining - 1));
                            tasks.push(Task::Value(item, target));
                            tasks.push(Task::Value(key, target));
                        }
                    }
                    Task::Set(obj, mut pos, mut parts, target, frozen, expected) => {
                        if ffi::PySet_Size(obj) != expected {
                            status = "failed".into();
                            break;
                        }
                        let mut key = std::ptr::null_mut();
                        let mut hash = 0;
                        if _PySet_NextEntry(obj, &mut pos, &mut key, &mut hash) != 0 {
                            let child = next_buffer;
                            next_buffer += 1;
                            if child == self.buffers.len() {
                                self.buffers.push(Vec::new())
                            }
                            parts.push(child);
                            tasks.push(Task::Set(obj, pos, parts, target, frozen, expected));
                            tasks.push(Task::Value(key, child));
                        } else {
                            let mut hashes: Vec<_> =
                                parts.iter().map(|i| digest(&self.buffers[*i])).collect();
                            hashes.sort_unstable();
                            self.buffers[target].push(if frozen { b'z' } else { b'e' });
                            size(&mut self.buffers[target], hashes.len());
                            for hash in hashes {
                                self.buffers[target].extend_from_slice(&hash)
                            }
                        }
                    }
                    Task::Resume(iterator, target) => {
                        protect(py, &tasks, &active, &mut keepers);
                        let snapshot = PyDict::new(py);
                        for (key, depth) in &active {
                            snapshot.set_item(*key, *depth)?;
                        }
                        let result = self
                            .fallback
                            .bind(py)
                            .call_method1("advance", (iterator, snapshot, visited))?;
                        let row = result.downcast::<PyTuple>()?;
                        self.apply(
                            py,
                            row,
                            target,
                            &mut tasks,
                            &mut active,
                            &mut keepers,
                            &mut visited,
                            &mut partial,
                            &mut immutable,
                            &mut status,
                            &mut normalized,
                            &mut delegated,
                            std::ptr::addr_of_mut!(ffi::PyBaseObject_Type),
                        )?;
                        if status != "complete" {
                            break;
                        }
                    }
                    Task::Value(obj, target) => {
                        visited += 1;
                        if max_nodes.is_some_and(|limit| visited > limit) {
                            status = "unknown".into();
                            break;
                        }
                        let kind = ffi::Py_TYPE(obj);
                        if obj == ffi::Py_None() {
                            self.buffers[target].push(b'N')
                        } else if kind == std::ptr::addr_of_mut!(ffi::PyBool_Type) {
                            self.buffers[target].push(if obj == ffi::Py_True() {
                                b'T'
                            } else {
                                b'F'
                            })
                        } else if kind == std::ptr::addr_of_mut!(ffi::PyLong_Type) {
                            let n = ffi::PyLong_AsLongLong(obj);
                            if !ffi::PyErr_Occurred().is_null() {
                                ffi::PyErr_Clear();
                                delegate = Some((obj, target))
                            } else {
                                self.buffers[target].push(b'i');
                                self.buffers[target].extend_from_slice(&n.to_le_bytes())
                            }
                        } else if kind == std::ptr::addr_of_mut!(ffi::PyFloat_Type) {
                            let f = ffi::PyFloat_AsDouble(obj);
                            self.buffers[target].push(b'f');
                            self.buffers[target].extend_from_slice(
                                &(if f.is_nan() {
                                    0x7ff8000000000000
                                } else {
                                    f.to_bits()
                                })
                                .to_le_bytes(),
                            );
                        } else if kind == std::ptr::addr_of_mut!(ffi::PyUnicode_Type) {
                            let object = Bound::from_borrowed_ptr(py, obj);
                            let data = unicode::payload(py, &object)?;
                            let decision = self.policy.secret(&data);
                            match decision {
                                Some(true) => {
                                    status = "redacted".into();
                                    break;
                                }
                                None => delegate = Some((obj, target)),
                                Some(false) => {
                                    let (bytes, count) = self.policy.normalize(&data);
                                    normalized += count;
                                    text(&mut self.buffers[target], b's', &bytes);
                                }
                            }
                        } else if self.special.iter().any(|t| t.as_ptr() == kind.cast()) {
                            delegate = Some((obj, target))
                        } else {
                            let list = kind == std::ptr::addr_of_mut!(ffi::PyList_Type);
                            let tuple = kind == std::ptr::addr_of_mut!(ffi::PyTuple_Type);
                            let dict = kind == std::ptr::addr_of_mut!(ffi::PyDict_Type);
                            let set = kind == std::ptr::addr_of_mut!(ffi::PySet_Type);
                            let frozen = kind == std::ptr::addr_of_mut!(ffi::PyFrozenSet_Type);
                            if list || tuple || dict || set || frozen {
                                if !tuple && !frozen {
                                    immutable = false
                                }
                                let count = if list {
                                    ffi::PyList_Size(obj)
                                } else if tuple {
                                    ffi::PyTuple_Size(obj)
                                } else {
                                    0
                                };
                                let first = if count > 0 {
                                    if list {
                                        ffi::PyList_GetItem(obj, 0)
                                    } else {
                                        ffi::PyTuple_GetItem(obj, 0)
                                    }
                                } else {
                                    std::ptr::null_mut()
                                };
                                let numeric_bulk = count as usize >= self.fast_min
                                    && !first.is_null()
                                    && [
                                        std::ptr::addr_of_mut!(ffi::PyLong_Type),
                                        std::ptr::addr_of_mut!(ffi::PyFloat_Type),
                                        std::ptr::addr_of_mut!(ffi::PyBool_Type),
                                    ]
                                    .contains(&ffi::Py_TYPE(first))
                                    && (0..count).all(|i| {
                                        ffi::Py_TYPE(if list {
                                            ffi::PyList_GetItem(obj, i)
                                        } else {
                                            ffi::PyTuple_GetItem(obj, i)
                                        }) == ffi::Py_TYPE(first)
                                    });
                                let string_bulk = count as usize >= self.fast_min
                                    && !first.is_null()
                                    && ffi::Py_TYPE(first)
                                        == std::ptr::addr_of_mut!(ffi::PyUnicode_Type)
                                    && (0..count).all(|i| {
                                        ffi::Py_TYPE(if list {
                                            ffi::PyList_GetItem(obj, i)
                                        } else {
                                            ffi::PyTuple_GetItem(obj, i)
                                        }) == std::ptr::addr_of_mut!(ffi::PyUnicode_Type)
                                    });
                                if numeric_bulk || string_bulk {
                                    self.bulk(
                                        py,
                                        obj,
                                        tuple,
                                        ffi::Py_TYPE(first),
                                        target,
                                        &tasks,
                                        &active,
                                        &mut keepers,
                                        &mut visited,
                                        &mut delegated,
                                        &mut normalized,
                                        &mut status,
                                        max_nodes,
                                    )?;
                                    if status != "complete" {
                                        break;
                                    }
                                    continue;
                                }
                                let address = obj as usize;
                                if let Some(depth) = active.get(&address) {
                                    self.buffers[target].push(b'@');
                                    size(&mut self.buffers[target], active.len() - depth);
                                    continue;
                                }
                                active.insert(address, active.len());
                                tasks.push(Task::Leave(address));
                                if set || frozen {
                                    tasks.push(Task::Set(
                                        obj,
                                        0,
                                        Vec::new(),
                                        target,
                                        frozen,
                                        ffi::PySet_Size(obj),
                                    ));
                                } else if dict {
                                    self.buffers[target].push(b'd');
                                    size(&mut self.buffers[target], ffi::PyDict_Size(obj) as usize);
                                    tasks.push(Task::Dict(
                                        obj,
                                        0,
                                        target,
                                        ffi::PyDict_Size(obj),
                                        ffi::PyDict_Size(obj),
                                    ));
                                } else {
                                    let count = if tuple {
                                        ffi::PyTuple_Size(obj)
                                    } else {
                                        ffi::PyList_Size(obj)
                                    };
                                    self.buffers[target].push(if tuple { b't' } else { b'l' });
                                    size(&mut self.buffers[target], count as usize);
                                    tasks.push(Task::Sequence(obj, 0, count, tuple, target));
                                }
                            } else if let Some(layout) = layout::get(
                                py,
                                kind,
                                self.enum_meta.as_ptr().cast(),
                                self.dict_getter,
                                || {
                                    keepers.push(Py::from_borrowed_ptr(py, obj));
                                    keepers.push(Py::from_borrowed_ptr(py, kind.cast()));
                                    for address in active.keys() {
                                        keepers.push(Py::from_borrowed_ptr(
                                            py,
                                            *address as *mut ffi::PyObject,
                                        ));
                                    }
                                    for pending in &tasks {
                                        match pending {
                                            Task::Value(p, _) | Task::Field(_, p, _)
                                                if !p.is_null() =>
                                            {
                                                keepers.push(Py::from_borrowed_ptr(py, *p))
                                            }
                                            _ => {}
                                        }
                                    }
                                },
                            )? {
                                if ffi::Py_TYPE(obj) != kind {
                                    delegate = Some((obj, target));
                                } else if let Some(mut state) = if layout.meta_object
                                    || !layout.has_dict
                                {
                                    Some(std::mem::take(&mut self.dict_buffer))
                                } else {
                                    layout::dictionary(obj, std::mem::take(&mut self.dict_buffer))
                                } {
                                    let address = obj as usize;
                                    if let Some(depth) = active.get(&address) {
                                        self.buffers[target].push(b'@');
                                        size(&mut self.buffers[target], active.len() - depth);
                                        continue;
                                    }
                                    immutable = false;
                                    active.insert(address, active.len());
                                    tasks.push(Task::Leave(address));
                                    let mut fields = std::mem::take(&mut self.fields);
                                    if layout.tuple {
                                        let count = ffi::PyTuple_Size(obj);
                                        for i in 0..count {
                                            let name = layout
                                                .tuple_names
                                                .as_ref()
                                                .filter(|names| names.len() == count as usize)
                                                .map(|names| names[i as usize].clone());
                                            fields.push((name, ffi::PyTuple_GetItem(obj, i)));
                                        }
                                    }
                                    for (name, offset) in &layout.slots {
                                        fields.push((
                                            Some(name.clone()),
                                            *(obj.cast::<u8>().offset(*offset)
                                                as *mut *mut ffi::PyObject),
                                        ))
                                    }
                                    for (key, item) in state.drain(..) {
                                        if ffi::Py_TYPE(key)
                                            == std::ptr::addr_of_mut!(ffi::PyUnicode_Type)
                                        {
                                            let name = if let Some((_, name)) =
                                                self.names.get(&(key as usize))
                                            {
                                                name.clone()
                                            } else {
                                                let name = Bound::from_borrowed_ptr(py, key);
                                                let bytes = Arc::new(
                                                    unicode::payload(py, &name)?.into_owned(),
                                                );
                                                self.names.insert(
                                                    key as usize,
                                                    (name.unbind(), bytes.clone()),
                                                );
                                                bytes
                                            };
                                            fields.push((Some(name), item));
                                        }
                                    }
                                    self.dict_buffer = state;
                                    text(&mut self.buffers[target], b'O', &layout.identity);
                                    size(&mut self.buffers[target], fields.len());
                                    for (name, item) in fields.drain(..).rev() {
                                        tasks.push(Task::Field(name, item, target));
                                    }
                                    self.fields = fields;
                                } else {
                                    delegate = Some((obj, target))
                                }
                            } else {
                                delegate = Some((obj, target))
                            }
                        }
                    }
                }
                if let Some((obj, target)) = delegate {
                    delegated += 1;
                    self.record_delegation(py, ffi::Py_TYPE(obj), 1);
                    protect(py, &tasks, &active, &mut keepers);
                    for address in active.keys() {
                        keepers.push(Py::from_borrowed_ptr(py, *address as *mut ffi::PyObject));
                    }
                    for pending in &tasks {
                        match pending {
                            Task::Value(p, _) | Task::Field(_, p, _) if !p.is_null() => {
                                keepers.push(Py::from_borrowed_ptr(py, *p))
                            }
                            _ => {}
                        }
                    }
                    let snapshot = PyDict::new(py);
                    for (key, depth) in &active {
                        snapshot.set_item(*key, *depth)?;
                    }
                    let object = Bound::from_borrowed_ptr(py, obj);
                    let result = self.fallback.bind(py).call_method1(
                        "start",
                        (object, snapshot, visited, partial_ok, max_nodes),
                    )?;
                    self.apply(
                        py,
                        result.downcast::<PyTuple>()?,
                        target,
                        &mut tasks,
                        &mut active,
                        &mut keepers,
                        &mut visited,
                        &mut partial,
                        &mut immutable,
                        &mut status,
                        &mut normalized,
                        &mut delegated,
                        std::ptr::addr_of_mut!(ffi::PyBaseObject_Type),
                    )?;
                    if status != "complete" {
                        break;
                    }
                }
            }
        }
        tasks.clear();
        active.clear();
        self.tasks = tasks;
        self.active = active;
        let hash = digest(&self.buffers[0]);
        let hex: String = hash.iter().map(|b| format!("{b:02x}")).collect();
        let result = PyDict::new(py);
        if wire {
            result.set_item("bytes", PyBytes::new(py, &self.buffers[0]))?;
        }
        result.set_item(
            "over_ceiling",
            max_nodes.is_some_and(|limit| visited > limit),
        )?;
        result.set_item("digest", hex)?;
        result.set_item("visited", visited)?;
        result.set_item("delegated", delegated)?;
        let types = PyDict::new(py);
        for (kind, count) in self.delegations.values() {
            types.set_item(kind.bind(py), count)?;
        }
        result.set_item("delegated_by_type", types)?;
        result.set_item("status", status)?;
        result.set_item("partial", partial)?;
        result.set_item("immutable", immutable)?;
        result.set_item("normalized", normalized)?;
        Ok(result.unbind())
    }
}
impl ReaderCore {
    #[allow(clippy::too_many_arguments)]
    unsafe fn bulk(
        &mut self,
        py: Python<'_>,
        obj: *mut ffi::PyObject,
        tuple: bool,
        expected: *mut ffi::PyTypeObject,
        target: usize,
        tasks: &[Task],
        active: &HashMap<usize, usize>,
        keepers: &mut Vec<Py<PyAny>>,
        visited: &mut usize,
        delegated: &mut usize,
        normalized: &mut usize,
        status: &mut String,
        max_nodes: Option<usize>,
    ) -> PyResult<()> {
        keepers.push(Py::from_borrowed_ptr(py, obj));
        let length = |obj| {
            if tuple {
                ffi::PyTuple_Size(obj)
            } else {
                ffi::PyList_Size(obj)
            }
        };
        self.buffers[target].push(if tuple { b't' } else { b'l' });
        size(&mut self.buffers[target], length(obj) as usize);
        self.string_buffer.clear();
        let mut pending: HashMap<usize, (Py<PyAny>, usize)> = HashMap::default();
        let mut index = 0;
        while index < length(obj) {
            let item = if tuple {
                ffi::PyTuple_GetItem(obj, index)
            } else {
                ffi::PyList_GetItem(obj, index)
            };
            let kind = ffi::Py_TYPE(item);
            let mut native = false;
            if expected == std::ptr::addr_of_mut!(ffi::PyLong_Type)
                && (kind == expected || kind == std::ptr::addr_of_mut!(ffi::PyBool_Type))
            {
                let number = ffi::PyLong_AsLongLong(item);
                if ffi::PyErr_Occurred().is_null() {
                    self.string_buffer.push(b'i');
                    self.string_buffer.extend_from_slice(&number.to_le_bytes());
                    native = true;
                } else {
                    ffi::PyErr_Clear();
                }
            } else if expected == std::ptr::addr_of_mut!(ffi::PyFloat_Type) && kind == expected {
                let number = ffi::PyFloat_AsDouble(item);
                self.string_buffer.push(b'f');
                self.string_buffer.extend_from_slice(
                    &(if number.is_nan() {
                        0x7ff8000000000000
                    } else {
                        number.to_bits()
                    })
                    .to_le_bytes(),
                );
                native = true;
            } else if expected == std::ptr::addr_of_mut!(ffi::PyBool_Type) && kind == expected {
                self.string_buffer
                    .push(if item == ffi::Py_True() { b'T' } else { b'F' });
                native = true;
            } else if expected == std::ptr::addr_of_mut!(ffi::PyUnicode_Type) && kind == expected {
                let object = Bound::from_borrowed_ptr(py, item);
                let data = unicode::payload(py, &object)?;
                match self.policy.secret(&data) {
                    Some(true) => {
                        *status = "redacted".into();
                        break;
                    }
                    Some(false) => {
                        text(&mut self.string_buffer, b's', &data);
                        native = true;
                    }
                    None => {}
                }
            }
            if !native {
                protect(py, tasks, active, keepers);
                let object = Bound::from_borrowed_ptr(py, item);
                pending
                    .entry(kind as usize)
                    .and_modify(|row| row.1 += 1)
                    .or_insert_with(|| (Py::from_borrowed_ptr(py, kind.cast()), 1));
                let result = self.fallback.bind(py).call_method1(
                    "bulk_leaf",
                    (object, Bound::from_borrowed_ptr(py, expected.cast())),
                )?;
                let row = result.downcast::<PyTuple>()?;
                *normalized += row.get_item(2)?.extract::<usize>()?;
                *status = row.get_item(1)?.extract()?;
                if status != "complete" {
                    break;
                }
                self.string_buffer
                    .extend_from_slice(row.get_item(0)?.downcast::<PyBytes>()?.as_bytes());
            }
            index += 1;
        }
        if status != "complete" {
            // Python's atomic bulk path charges only its container on failure.
            if !pending.is_empty() {
                *delegated += 1;
                self.record_delegation(py, ffi::Py_TYPE(obj), 1);
            }
            return Ok(());
        }
        *visited += length(obj) as usize;
        for (kind, count) in pending.into_values() {
            *delegated += count;
            self.record_delegation(py, kind.as_ptr().cast(), count);
        }
        if max_nodes.is_some_and(|limit| *visited > limit) {
            *status = "unknown".into();
            return Ok(());
        }
        self.buffers[target].extend_from_slice(&self.string_buffer);
        Ok(())
    }
    unsafe fn record_delegation(
        &mut self,
        py: Python<'_>,
        kind: *mut ffi::PyTypeObject,
        count: usize,
    ) {
        if count == 0 {
            return;
        }
        self.delegations
            .entry(kind as usize)
            .and_modify(|row| row.1 += count)
            .or_insert_with(|| (Py::from_borrowed_ptr(py, kind.cast()), count));
    }
    #[allow(clippy::too_many_arguments)]
    fn apply(
        &mut self,
        py: Python<'_>,
        row: &Bound<'_, PyTuple>,
        target: usize,
        tasks: &mut Vec<Task>,
        active: &mut HashMap<usize, usize>,
        keepers: &mut Vec<Py<PyAny>>,
        visited: &mut usize,
        partial: &mut bool,
        immutable: &mut bool,
        status: &mut String,
        normalized: &mut usize,
        delegated: &mut usize,
        extra_kind: *mut ffi::PyTypeObject,
    ) -> PyResult<()> {
        let bytes = row.get_item(0)?;
        self.buffers[target].extend_from_slice(bytes.downcast::<PyBytes>()?.as_bytes());
        let next: usize = row.get_item(4)?.extract()?;
        let extra = next.saturating_sub(*visited);
        *delegated += extra;
        unsafe {
            self.record_delegation(py, extra_kind, extra);
        }
        *visited = next;
        *active = row.get_item(5)?.extract()?;
        *partial |= row.get_item(6)?.extract::<bool>()?;
        *immutable &= row.get_item(7)?.extract::<bool>()?;
        *normalized += row.get_item(8)?.extract::<usize>()?;
        let state = row.get_item(3)?.extract::<String>()?;
        if state == "child" {
            let iterator = row.get_item(1)?.unbind();
            let child = row.get_item(2)?.unbind();
            let ptr = child.as_ptr();
            keepers.push(child);
            tasks.push(Task::Resume(iterator, target));
            tasks.push(Task::Value(ptr, target));
            *status = "complete".into();
        } else {
            *status = state;
        }
        let _ = py;
        Ok(())
    }
}

// Every borrowed container and pending child must survive a Python callback.
unsafe fn protect(
    py: Python<'_>,
    tasks: &[Task],
    active: &HashMap<usize, usize>,
    keepers: &mut Vec<Py<PyAny>>,
) {
    for address in active.keys() {
        keepers.push(Py::from_borrowed_ptr(py, *address as *mut ffi::PyObject));
    }
    for task in tasks {
        let object = match task {
            Task::Value(p, _)
            | Task::Field(_, p, _)
            | Task::Sequence(p, ..)
            | Task::Dict(p, ..)
            | Task::Set(p, ..) => *p,
            _ => std::ptr::null_mut(),
        };
        if !object.is_null() {
            keepers.push(Py::from_borrowed_ptr(py, object));
        }
    }
}
