//! Port of scripts/ade_t3.py: not ported yet.
use crate::py::{self, Res};
use serde_json::Value;

pub fn call(_args: &[String]) -> Res<Value> {
    py::err("ade_t3: not ported yet")
}
