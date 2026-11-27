//! UTF-8/surrogatepass access only. Policy and normalization are not applied here;
//! this helper must not be used as a complete signature encoder.
use pyo3::{exceptions::PyTypeError, ffi, prelude::*, types::PyBytes};
use std::borrow::Cow;

/// Borrow CPython's UTF-8 cache where available. Surrogates cannot populate
/// that cache, so encode those strings from CPython's canonical code units.
/// The caller holds the GIL and an owning reference for the returned lifetime.
unsafe fn payload<'a>(py: Python<'_>, value: &'a Bound<'_, PyAny>) -> PyResult<Cow<'a, [u8]>> {
    let object = value.as_ptr();
    if ffi::Py_TYPE(object) != std::ptr::addr_of_mut!(ffi::PyUnicode_Type) {
        return Err(PyTypeError::new_err("expected an exact str"));
    }
    let mut length = 0;
    let utf8 = ffi::PyUnicode_AsUTF8AndSize(object, &mut length);
    if !utf8.is_null() {
        return Ok(Cow::Borrowed(std::slice::from_raw_parts(
            utf8.cast(),
            length as usize,
        )));
    }
    // Do not turn allocation failures or other interpreter errors into an
    // encoding fallback. Only surrogate-containing strings take this path.
    if ffi::PyErr_ExceptionMatches(ffi::PyExc_UnicodeEncodeError) == 0 {
        return Err(PyErr::fetch(py));
    }
    ffi::PyErr_Clear();
    let count = ffi::PyUnicode_GET_LENGTH(object) as usize;
    let kind = ffi::PyUnicode_KIND(object);
    let data = ffi::PyUnicode_DATA(object);
    let mut out = Vec::with_capacity(count);
    for index in 0..count {
        let code = match kind {
            1 => *data.cast::<u8>().add(index) as u32,
            2 => *data.cast::<u16>().add(index) as u32,
            4 => *data.cast::<u32>().add(index),
            _ => unreachable!("invalid CPython Unicode kind"),
        };
        if code < 0x80 {
            out.push(code as u8);
        } else if code < 0x800 {
            out.extend_from_slice(&[(0xc0 | (code >> 6)) as u8, (0x80 | (code & 63)) as u8]);
        } else if code < 0x10000 {
            out.extend_from_slice(&[
                (0xe0 | (code >> 12)) as u8,
                (0x80 | ((code >> 6) & 63)) as u8,
                (0x80 | (code & 63)) as u8,
            ]);
        } else {
            out.extend_from_slice(&[
                (0xf0 | (code >> 18)) as u8,
                (0x80 | ((code >> 12) & 63)) as u8,
                (0x80 | ((code >> 6) & 63)) as u8,
                (0x80 | (code & 63)) as u8,
            ]);
        }
    }
    Ok(Cow::Owned(out))
}

#[pyfunction]
pub fn unicode_payload(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Py<PyBytes>> {
    let bytes = unsafe { payload(py, value)? };
    Ok(PyBytes::new(py, &bytes).unbind())
}
