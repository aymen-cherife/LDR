//! CPython-version-specific, GIL-held layout access. No managed-dict materialization.
use crate::unicode;
use pyo3::{ffi, prelude::*};
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicI32, AtomicU64, Ordering},
        Arc, Mutex, OnceLock,
    },
};
static WATCHER: AtomicI32 = AtomicI32::new(-1);
static EPOCH: AtomicU64 = AtomicU64::new(0);
static CACHE: OnceLock<Mutex<(u64, HashMap<usize, Arc<Layout>>)>> = OnceLock::new();
extern "C" {
    fn _PyType_Lookup(kind: *mut ffi::PyTypeObject, name: *mut ffi::PyObject)
        -> *mut ffi::PyObject;
    fn PyType_AddWatcher(callback: unsafe extern "C" fn(*mut ffi::PyObject) -> i32) -> i32;
    fn PyType_Watch(id: i32, cls: *mut ffi::PyObject) -> i32;
}
unsafe extern "C" fn changed(_: *mut ffi::PyObject) -> i32 {
    EPOCH.fetch_add(1, Ordering::Relaxed);
    0
}
#[pyclass]
struct Gone {
    address: usize,
}
#[pymethods]
impl Gone {
    fn __call__(&self, _weak: &Bound<'_, PyAny>) {
        if let Some(c) = CACHE.get() {
            c.lock().unwrap().1.remove(&self.address);
        }
    }
}
pub struct Layout {
    pub identity: Vec<u8>,
    pub slots: Vec<(Arc<Vec<u8>>, isize)>,
    pub _weak: Py<PyAny>,
    declarations: Vec<(usize, Vec<Option<Py<PyAny>>>)>,
}
#[repr(C)]
struct Member {
    name: *const i8,
    kind: i32,
    offset: isize,
    flags: i32,
    doc: *const i8,
}
#[repr(C)]
struct Keys {
    refs: isize,
    log_size: u8,
    log_bytes: u8,
    kind: u8,
    version: u32,
    usable: isize,
    entries: isize,
}
#[repr(C)]
struct Entry {
    key: *mut ffi::PyObject,
    value: *mut ffi::PyObject,
}
unsafe fn bytes(py: Python<'_>, p: *mut ffi::PyObject) -> PyResult<Vec<u8>> {
    let obj = Bound::from_borrowed_ptr(py, p);
    Ok(unicode::payload(py, &obj)?.into_owned())
}
pub fn watcher_count() -> i32 {
    if WATCHER.load(Ordering::Relaxed) >= 0 {
        1
    } else {
        0
    }
}
pub unsafe fn get(py: Python<'_>, kind: *mut ffi::PyTypeObject) -> PyResult<Option<Arc<Layout>>> {
    if WATCHER.load(Ordering::Relaxed) < 0 {
        let id = PyType_AddWatcher(changed);
        if id < 0 {
            return Err(PyErr::fetch(py));
        }
        WATCHER.store(id, Ordering::Relaxed);
    }
    let c = CACHE.get_or_init(|| Mutex::new((0, HashMap::new())));
    {
        let mut cache = c.lock().unwrap();
        let epoch = EPOCH.load(Ordering::Relaxed);
        if cache.0 != epoch {
            cache.1.clear();
            cache.0 = epoch;
        }
        if let Some(layout) = cache.1.get(&(kind as usize)) {
            let valid = layout.declarations.iter().all(|(pointer, items)| {
                let list = *pointer as *mut ffi::PyObject;
                ffi::PyList_Size(list) as usize == items.len()
                    && items.iter().enumerate().all(|(i, old)| {
                        let current = ffi::PyList_GetItem(list, i as isize);
                        match old {
                            Some(old) => old.as_ptr() == current,
                            None => {
                                ffi::Py_TYPE(current) != std::ptr::addr_of_mut!(ffi::PyUnicode_Type)
                            }
                        }
                    })
            });
            if valid {
                return Ok(Some(layout.clone()));
            }
        }
        cache.1.remove(&(kind as usize));
    }
    // Only ordinary Python classes; metaclasses, container subclasses, C bases,
    // and custom attribute hooks use the exact Python node reader.
    if ffi::Py_TYPE(kind.cast()) != std::ptr::addr_of_mut!(ffi::PyType_Type)
        || (*kind).tp_flags & 512 == 0
        || (*kind).tp_getattro.map(|f| f as usize)
            != Some(ffi::PyObject_GenericGetAttr as *const () as usize)
    {
        return Ok(None);
    }
    let mro = (*kind).tp_mro;
    let mut slots = Vec::new();
    let mut declared_state = false;
    let mut declarations = Vec::new();
    for index in (0..ffi::PyTuple_Size(mro)).rev() {
        let owner = ffi::PyTuple_GetItem(mro, index) as *mut ffi::PyTypeObject;
        if owner == std::ptr::addr_of_mut!(ffi::PyBaseObject_Type) {
            continue;
        }
        if (*owner).tp_flags & 512 == 0 {
            return Ok(None);
        }
        if PyType_Watch(WATCHER.load(Ordering::Relaxed), owner.cast()) < 0 {
            return Err(PyErr::fetch(py));
        }
        // Populate a valid type version so subclass invalidation notifies watchers.
        let class_name = ffi::PyUnicode_InternFromString(c"__class__".as_ptr());
        _PyType_Lookup(owner, class_name);
        ffi::Py_DECREF(class_name);
        let ns = (*owner).tp_dict;
        let declaration = ffi::PyDict_GetItemString(ns, c"__slots__".as_ptr());
        if declaration.is_null() {
            continue;
        }
        declared_state = true;
        let mut position = 0;
        let mut key = std::ptr::null_mut();
        let mut value = std::ptr::null_mut();
        let owner_name = Bound::from_owned_ptr(py, ffi::PyType_GetName(owner));
        if ffi::Py_TYPE(owner_name.as_ptr()) != std::ptr::addr_of_mut!(ffi::PyUnicode_Type) {
            return Ok(None);
        }
        let owner_bytes = unicode::payload(py, &owner_name)?;
        let trimmed = &owner_bytes[owner_bytes.iter().take_while(|b| **b == b'_').count()..];
        let prefix = [b"_".as_slice(), trimmed, b"__".as_slice()].concat();
        let mut declared = HashMap::new();
        let mut names = Vec::new();
        let declaration_kind = ffi::Py_TYPE(declaration);
        if declaration_kind == std::ptr::addr_of_mut!(ffi::PyUnicode_Type) {
            names.push(declaration)
        } else if declaration_kind == std::ptr::addr_of_mut!(ffi::PyList_Type)
            || declaration_kind == std::ptr::addr_of_mut!(ffi::PyTuple_Type)
        {
            let list = declaration_kind == std::ptr::addr_of_mut!(ffi::PyList_Type);
            let count = if list {
                ffi::PyList_Size(declaration)
            } else {
                ffi::PyTuple_Size(declaration)
            };
            let mut snapshot = Vec::new();
            for i in 0..count {
                let item = if list {
                    ffi::PyList_GetItem(declaration, i)
                } else {
                    ffi::PyTuple_GetItem(declaration, i)
                };
                if ffi::Py_TYPE(item) == std::ptr::addr_of_mut!(ffi::PyUnicode_Type) {
                    names.push(item);
                    snapshot.push(Some(Py::from_borrowed_ptr(py, item)));
                } else {
                    snapshot.push(None)
                }
            }
            if list {
                declarations.push((declaration as usize, snapshot));
            }
        }
        for item in names {
            let name = bytes(py, item)?;
            let stored = if name.starts_with(b"__") && !name.ends_with(b"__") {
                [b"_".as_slice(), trimmed, &name].concat()
            } else {
                name.clone()
            };
            declared.insert(stored, name);
        }
        while ffi::PyDict_Next(ns, &mut position, &mut key, &mut value) != 0 {
            if ffi::Py_TYPE(value) != std::ptr::addr_of_mut!(ffi::PyMemberDescr_Type) {
                continue;
            }
            let descriptor = value as *mut ffi::PyMemberDescrObject;
            if (*descriptor).d_common.d_type != owner {
                continue;
            }
            if _PyType_Lookup(kind, key) != value {
                return Ok(None);
            }
            let member = (*descriptor).d_member as *mut Member;
            if (*member).kind != 6 && (*member).kind != 16 {
                return Ok(None);
            }
            let name = bytes(py, key)?;
            if name == b"__dict__" || name == b"__weakref__" {
                continue;
            }
            let name = if let Some(original) = declared.get(&name) {
                original.clone()
            } else if name.starts_with(&prefix) && !name.ends_with(b"__") {
                [b"__".as_slice(), &name[prefix.len()..]].concat()
            } else {
                name
            };
            slots.push((Arc::new(name), (*member).offset));
        }
    }
    if !declared_state && (*kind).tp_dictoffset == 0 && (*kind).tp_flags & 16 == 0 {
        return Ok(None);
    }
    let module = ffi::PyDict_GetItemString((*kind).tp_dict, c"__module__".as_ptr());
    if module.is_null() || ffi::Py_TYPE(module) != std::ptr::addr_of_mut!(ffi::PyUnicode_Type) {
        return Ok(None);
    }
    let mut identity = bytes(py, module)?;
    identity.push(b'.');
    let qualname = Bound::from_owned_ptr(py, ffi::PyType_GetQualName(kind));
    if ffi::Py_TYPE(qualname.as_ptr()) != std::ptr::addr_of_mut!(ffi::PyUnicode_Type) {
        return Ok(None);
    }
    identity.extend_from_slice(&unicode::payload(py, &qualname)?);
    let callback = Py::new(
        py,
        Gone {
            address: kind as usize,
        },
    )?;
    let weak = ffi::PyWeakref_NewRef(kind.cast(), callback.as_ptr());
    if weak.is_null() {
        return Err(PyErr::fetch(py));
    }
    let layout = Arc::new(Layout {
        identity,
        slots,
        _weak: Py::from_owned_ptr(py, weak),
        declarations,
    });
    c.lock().unwrap().1.insert(kind as usize, layout.clone());
    Ok(Some(layout))
}
// Borrow the instance's actual dictionary or its split values and shared keys.
// Order is CPython's insertion-order array, not the shared-key allocation order.
pub unsafe fn dictionary(
    obj: *mut ffi::PyObject,
    mut result: Vec<(*mut ffi::PyObject, *mut ffi::PyObject)>,
) -> Option<Vec<(*mut ffi::PyObject, *mut ffi::PyObject)>> {
    let kind = ffi::Py_TYPE(obj);
    result.clear();
    if (*kind).tp_flags & 16 != 0 {
        let pointer = (obj as *mut usize).offset(-3).read();
        #[cfg(not(Py_3_13))]
        let (dict, values) = if pointer & 1 != 0 {
            (std::ptr::null_mut(), (pointer + 1) as *mut u8)
        } else {
            (pointer as *mut ffi::PyObject, std::ptr::null_mut())
        };
        #[cfg(Py_3_13)]
        let (dict, values) = if pointer != 0 {
            (pointer as *mut ffi::PyObject, std::ptr::null_mut())
        } else if (*kind).tp_flags & (1 << 2) != 0 {
            (
                std::ptr::null_mut(),
                (obj as *mut u8).add(std::mem::size_of::<ffi::PyObject>()),
            )
        } else {
            return Some(result);
        };
        if !dict.is_null() {
            return Some(dict_items(dict, result));
        }
        if values.is_null() {
            return Some(result);
        }
        let keys = (*(kind as *mut ffi::PyHeapTypeObject)).ht_cached_keys as *mut Keys;
        if keys.is_null() {
            return None;
        }
        let entries = (keys as *mut u8)
            .add(std::mem::size_of::<Keys>() + (1usize << (*keys).log_bytes))
            as *mut Entry;
        #[cfg(not(Py_3_13))]
        let (size, items, order) = (
            *values.offset(-2) as usize,
            values as *mut *mut ffi::PyObject,
            values.offset(-3),
        );
        #[cfg(Py_3_13)]
        let (size, items, order) = {
            if *values.add(3) == 0 {
                return None;
            }
            let items = values.add(8) as *mut *mut ffi::PyObject;
            (
                *values.add(1) as usize,
                items,
                items.add(*values as usize) as *mut u8,
            )
        };
        for i in 0..size {
            #[cfg(not(Py_3_13))]
            let index = *order.offset(-(i as isize)) as usize;
            #[cfg(Py_3_13)]
            let index = *order.add(i) as usize;
            let value = *items.add(index);
            if !value.is_null() {
                result.push(((*entries.add(index)).key, value))
            }
        }
        Some(result)
    } else {
        let offset = (*kind).tp_dictoffset;
        if offset < 0 {
            return None;
        }
        if offset == 0 {
            return Some(result);
        }
        let dict = *(obj.cast::<u8>().offset(offset) as *mut *mut ffi::PyObject);
        if dict.is_null() {
            Some(result)
        } else {
            Some(dict_items(dict, result))
        }
    }
}
unsafe fn dict_items(
    dict: *mut ffi::PyObject,
    mut rows: Vec<(*mut ffi::PyObject, *mut ffi::PyObject)>,
) -> Vec<(*mut ffi::PyObject, *mut ffi::PyObject)> {
    rows.clear();
    let mut pos = 0;
    let mut key = std::ptr::null_mut();
    let mut value = std::ptr::null_mut();
    while ffi::PyDict_Next(dict, &mut pos, &mut key, &mut value) != 0 {
        rows.push((key, value))
    }
    rows
}
