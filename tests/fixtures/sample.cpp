// C++ fixture for symbol extraction tests
#include <iostream>
#include <string>

class Calculator {
private:
    int value;

public:
    Calculator(int initial) : value(initial) {}

    void add(int amount) {
        value += amount;
    }

    int getValue() const {
        return value;
    }
};

struct Config {
    std::string host;
    int port;
};

void print_hello() {
    std::cout << "Hello" << std::endl;
}

int add(int a, int b) {
    return a + b;
}