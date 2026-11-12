//! Port of scripts/ade_cmux.py: not ported yet.
use crate::py::{self, Res};
use serde_json::Value;

pub fn call(_args: &[String]) -> Res<Value> {
    py::err("ade_cmux: not ported yet")
}
