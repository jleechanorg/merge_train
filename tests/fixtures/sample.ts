// TypeScript fixture for symbol extraction tests
export function add(a: number, b: number): number {
    return a + b;
}

export const multiply = (x: number, y: number): number => x * y;

export class Calculator {
    private value: number;

    constructor(initial: number = 0) {
        this.value = initial;
    }

    public add(amount: number): void {
        this.value += amount;
    }

    public getValue(): number {
        return this.value;
    }
}

interface Config {
    host: string;
    port: number;
}

type UserId = string;

export async function fetchUser(id: UserId): Promise<Config> {
    return { host: "localhost", port: 8080 };
}

export const CONFIG: Config = { host: "127.0.0.1", port: 3000 };