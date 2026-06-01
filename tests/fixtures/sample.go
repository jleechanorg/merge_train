// Go fixture for symbol extraction tests
package main

type Config struct {
    Host string
    Port int
}

func main() {
    println("hello")
}

func Add(a int, b int) int {
    return a + b
}

func (c *Config) GetHost() string {
    return c.Host
}

type User struct {
    Name string
}

func (u *User) Greet() string {
    return "Hello, " + u.Name
}

type Calculator struct {
    value int
}

func (c *Calculator) Add(amount int) {
    c.value += amount
}

func (c *Calculator) GetValue() int {
    return c.value
}