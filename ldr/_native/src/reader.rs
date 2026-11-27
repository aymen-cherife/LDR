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
    Set(*mut ffi::PyObject, isize, Vec<usize>, usize, bool),
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
        })
    }
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let d = PyDict::new(py);
        d.set_item("watchers", layout::watcher_count())?;
        Ok(d.unbind())
    }
    fn walk(
        &mut self,
        py: Python<'_>,
        value: &Bound<'_, PyAny>,
        partial_ok: bool,
    ) -> PyResult<Py<PyDict>> {
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
                                        delegated += 1
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
                    Task::Set(obj, mut pos, mut parts, target, frozen) => {
                        let mut key = std::ptr::null_mut();
                        let mut hash = 0;
                        if _PySet_NextEntry(obj, &mut pos, &mut key, &mut hash) != 0 {
                            let child = next_buffer;
                            next_buffer += 1;
                            if child == self.buffers.len() {
                                self.buffers.push(Vec::new())
                            }
                            parts.push(child);
                            tasks.push(Task::Set(obj, pos, parts, target, frozen));
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
                        )?;
                        if status != "complete" {
                            break;
                        }
                    }
                    Task::Value(obj, target) => {
                        visited += 1;
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
                                let address = obj as usize;
                                if let Some(depth) = active.get(&address) {
                                    self.buffers[target].push(b'@');
                                    size(&mut self.buffers[target], active.len() - depth);
                                    continue;
                                }
                                active.insert(address, active.len());
                                tasks.push(Task::Leave(address));
                                if set || frozen {
                                    tasks.push(Task::Set(obj, 0, Vec::new(), target, frozen));
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
                                    let string_run = count as usize >= self.fast_min
                                        && (0..count).all(|i| {
                                            ffi::Py_TYPE(if tuple {
                                                ffi::PyTuple_GetItem(obj, i)
                                            } else {
                                                ffi::PyList_GetItem(obj, i)
                                            }) == std::ptr::addr_of_mut!(ffi::PyUnicode_Type)
                                        });
                                    if string_run {
                                        self.string_buffer.clear();
                                        let mut secret = false;
                                        let mut string_delegated = 0;
                                        let mut string_normalized = 0;
                                        for i in 0..count {
                                            let item = if tuple {
                                                ffi::PyTuple_GetItem(obj, i)
                                            } else {
                                                ffi::PyList_GetItem(obj, i)
                                            };
                                            if item.is_null() {
                                                ffi::PyErr_Clear();
                                                status = "failed".into();
                                                break;
                                            }
                                            let object = Bound::from_borrowed_ptr(py, item);
                                            let data = unicode::payload(py, &object)?;
                                            let found = match self.policy.secret(&data) {
                                                Some(found) => found,
                                                None => {
                                                    for address in active.keys() {
                                                        keepers.push(Py::from_borrowed_ptr(
                                                            py,
                                                            *address as *mut ffi::PyObject,
                                                        ));
                                                    }
                                                    for pending in &tasks {
                                                        match pending {
                                                            Task::Value(p, _)
                                                            | Task::Field(_, p, _)
                                                                if !p.is_null() =>
                                                            {
                                                                keepers.push(Py::from_borrowed_ptr(
                                                                    py, *p,
                                                                ))
                                                            }
                                                            _ => {}
                                                        }
                                                    }
                                                    string_delegated += 1;
                                                    self.fallback
                                                        .bind(py)
                                                        .call_method1(
                                                            "secret_text",
                                                            (object.clone(),),
                                                        )?
                                                        .extract()?
                                                }
                                            };
                                            if found {
                                                secret = true;
                                                break;
                                            }
                                            let (bytes, number) = self.policy.normalize(&data);
                                            string_normalized += number;
                                            text(&mut self.string_buffer, b's', &bytes);
                                        }
                                        normalized += string_normalized;
                                        if status != "complete" {
                                            break;
                                        }
                                        if secret {
                                            if string_delegated > 0 {
                                                delegated += 1
                                            }
                                            status = "redacted".into();
                                            break;
                                        }
                                        delegated += string_delegated;
                                        visited += count as usize;
                                        self.buffers[target].extend_from_slice(&self.string_buffer);
                                    } else {
                                        tasks.push(Task::Sequence(obj, 0, count, tuple, target));
                                    }
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
                    let result = self
                        .fallback
                        .bind(py)
                        .call_method1("start", (object, snapshot, visited, partial_ok))?;
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
        result.set_item("bytes", PyBytes::new(py, &self.buffers[0]))?;
        result.set_item("digest", hex)?;
        result.set_item("visited", visited)?;
        result.set_item("delegated", delegated)?;
        result.set_item("status", status)?;
        result.set_item("partial", partial)?;
        result.set_item("immutable", immutable)?;
        result.set_item("normalized", normalized)?;
        Ok(result.unbind())
    }
}
impl ReaderCore {
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
    ) -> PyResult<()> {
        let bytes = row.get_item(0)?;
        self.buffers[target].extend_from_slice(bytes.downcast::<PyBytes>()?.as_bytes());
        *visited = row.get_item(4)?.extract()?;
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
