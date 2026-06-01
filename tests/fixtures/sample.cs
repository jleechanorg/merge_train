// C# fixture for symbol extraction tests
using System;

public class Calculator
{
    private int value;

    public Calculator(int initial)
    {
        this.value = initial;
    }

    public void Add(int amount)
    {
        this.value += amount;
    }

    public int GetValue()
    {
        return this.value;
    }

    public static int Multiply(int a, int b)
    {
        return a * b;
    }
}

class Config
{
    public string Host { get; set; }
    public int Port { get; set; }
}