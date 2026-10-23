// HashMap's random keys: std seeds them from the kernel (getrandom) at
// startup, so each run hashes the same value differently.
use std::collections::hash_map::RandomState;
use std::hash::BuildHasher;

fn main() {
    println!("{}", RandomState::new().hash_one(42u64));
}
