// Java fixture for symbol extraction tests
public class Calculator {
    private int value;

    public Calculator(int initial) {
        this.value = initial;
    }

    public void add(int amount) {
        this.value += amount;
    }

    public int getValue() {
        return this.value;
    }

    public static int multiply(int a, int b) {
        return a * b;
    }
}

class Config {
    String host;
    int port;
}