//! greenlight: one binary for what were scripts/*.py. `greenlight <script> args...` runs that script
//! (dash, chat, adopt, cleanup, auto, blocking); anything else is greenlight.py's own CLI.
mod ade_cmux;
mod ade_common;
mod ade_herdr;
mod ade_t3;
mod adopt;
mod auto;
mod base;
mod blocking;
mod chat;
mod cleanup;
mod dash;
mod greenlight;
mod harness;
mod py;

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let script = match args.first().map(String::as_str) {
        Some("greenlight" | "dash" | "chat" | "adopt" | "cleanup" | "auto" | "blocking") => args.remove(0),
        _ => "greenlight".into(),
    };
    if let Err(e) = base::init() {
        py::die_traceback(&e)
    }
    let r = match script.as_str() {
        "dash" => dash::main(args),
        "chat" => chat::main(args),
        "adopt" => adopt::main(args),
        "cleanup" => cleanup::main(args),
        "auto" => auto::main(args),
        "blocking" => blocking::main(args),
        _ => greenlight::main(args),
    };
    use std::io::Write;
    let _ = std::io::stdout().flush();
    match r {
        Ok(rc) => std::process::exit(rc),
        Err(e) => py::die_traceback(&e),
    }
}
