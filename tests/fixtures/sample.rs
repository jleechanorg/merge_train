// Rust fixture for symbol extraction tests
pub struct Config {
    host: String,
    port: u16,
}

impl Config {
    pub fn new(host: &str, port: u16) -> Self {
        Config { host: host.to_string(), port }
    }

    pub fn get_host(&self) -> &str {
        &self.host
    }
}

fn add(a: i32, b: i32) -> i32 {
    a + b
}

struct Calculator {
    value: i32,
}

impl Calculator {
    fn new() -> Self {
        Calculator { value: 0 }
    }

    fn add(&mut self, amount: i32) {
        self.value += amount;
    }

    fn get_value(&self) -> i32 {
        self.value
    }
}

pub fn main() {
    println!("Hello");
}