//! Standalone numeric/container core. Not registered as LDR's runtime reader yet.
mod layout;
mod policy;
mod reader;
mod unicode;

use blake2::{
    digest::{Update, VariableOutput},
    Blake2bVar,
};
use pyo3::{exceptions::PyTypeError, ffi, prelude::*, types::PyBytes};
use std::collections::HashMap;

enum Task {
    Value(*mut ffi::PyObject),
    Leave(usize),
    Sequence(*mut ffi::PyObject, isize, isize, bool),
    Dict(*mut ffi::PyObject, isize),
}

fn length(out: &mut Vec<u8>, n: usize) {
    out.extend_from_slice(&(n as u64).to_le_bytes());
}

#[pyfunction]
fn numeric_walk(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
) -> PyResult<(Py<PyBytes>, String, usize)> {
    // The root owns the graph and the GIL is held. No callbacks or Python code
    // are invoked, so borrowed child pointers remain valid for the entire pass.
    let mut tasks = vec![Task::Value(value.as_ptr())];
    let mut active = HashMap::new();
    let mut out = Vec::new();
    let mut visited = 0usize;
    unsafe {
        while let Some(task) = tasks.pop() {
            match task {
                Task::Leave(address) => {
                    active.remove(&address);
                }
                Task::Sequence(obj, index, count, tuple) => {
                    if index < count {
                        tasks.push(Task::Sequence(obj, index + 1, count, tuple));
                        let item = if tuple {
                            ffi::PyTuple_GetItem(obj, index)
                        } else {
                            ffi::PyList_GetItem(obj, index)
                        };
                        tasks.push(Task::Value(item));
                    }
                }
                Task::Dict(obj, mut position) => {
                    let mut key = std::ptr::null_mut();
                    let mut item = std::ptr::null_mut();
                    if ffi::PyDict_Next(obj, &mut position, &mut key, &mut item) != 0 {
                        tasks.push(Task::Dict(obj, position));
                        tasks.push(Task::Value(item));
                        tasks.push(Task::Value(key));
                    }
                }
                Task::Value(obj) => {
                    visited += 1;
                    let kind = ffi::Py_TYPE(obj);
                    if obj == ffi::Py_None() {
                        out.push(b'N');
                    } else if kind == std::ptr::addr_of_mut!(ffi::PyBool_Type) {
                        out.push(if obj == ffi::Py_True() { b'T' } else { b'F' });
                    } else if kind == std::ptr::addr_of_mut!(ffi::PyLong_Type) {
                        let number = ffi::PyLong_AsLongLong(obj);
                        if !ffi::PyErr_Occurred().is_null() {
                            return Err(PyErr::fetch(py));
                        }
                        out.push(b'i');
                        out.extend_from_slice(&number.to_le_bytes());
                    } else if kind == std::ptr::addr_of_mut!(ffi::PyFloat_Type) {
                        let number = ffi::PyFloat_AsDouble(obj);
                        let bits = if number.is_nan() {
                            0x7ff8000000000000u64
                        } else {
                            number.to_bits()
                        };
                        out.push(b'f');
                        out.extend_from_slice(&bits.to_le_bytes());
                    } else {
                        let list = kind == std::ptr::addr_of_mut!(ffi::PyList_Type);
                        let tuple = kind == std::ptr::addr_of_mut!(ffi::PyTuple_Type);
                        let dict = kind == std::ptr::addr_of_mut!(ffi::PyDict_Type);
                        if !list && !tuple && !dict {
                            return Err(PyTypeError::new_err(
                                "type outside the standalone numeric/container core",
                            ));
                        }
                        let address = obj as usize;
                        if let Some(depth) = active.get(&address) {
                            out.push(b'@');
                            length(&mut out, active.len() - depth);
                            continue;
                        }
                        active.insert(address, active.len());
                        tasks.push(Task::Leave(address));
                        if dict {
                            out.push(b'd');
                            length(&mut out, ffi::PyDict_Size(obj) as usize);
                            tasks.push(Task::Dict(obj, 0));
                        } else {
                            let count = if tuple {
                                ffi::PyTuple_Size(obj)
                            } else {
                                ffi::PyList_Size(obj)
                            };
                            out.push(if tuple { b't' } else { b'l' });
                            length(&mut out, count as usize);
                            tasks.push(Task::Sequence(obj, 0, count, tuple));
                        }
                    }
                }
            }
        }
    }
    let mut hash = Blake2bVar::new(16).unwrap();
    hash.update(&out);
    let mut digest = [0u8; 16];
    hash.finalize_variable(&mut digest).unwrap();
    let hex = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok((PyBytes::new(py, &out).unbind(), hex, visited))
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    layout::initialize(module.py())?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add_function(wrap_pyfunction!(watcher_count, module)?)?;
    module.add_function(wrap_pyfunction!(_test_panic, module)?)?;
    module.add_class::<reader::ReaderCore>()?;
    module.add_function(wrap_pyfunction!(numeric_walk, module)?)?;
    module.add_function(wrap_pyfunction!(unicode::unicode_payload, module)?)?;
    Ok(())
}

#[pyfunction]
fn watcher_count() -> i32 { layout::watcher_count() }

#[pyfunction]
fn _test_panic() { panic!("injected native panic"); }
