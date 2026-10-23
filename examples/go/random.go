// The global math/rand generator: Go seeds it at startup, from the
// kernel's random bytes, so each run draws its own numbers.
package main

import (
	"fmt"
	"math/rand"
)

func main() {
	fmt.Println(rand.Int63())
}
