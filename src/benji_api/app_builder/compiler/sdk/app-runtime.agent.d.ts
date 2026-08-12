type JsonObject = Record<string, unknown>;
type RequestOptions = {
    idempotencyKey?: string;
    timeoutMs?: number;
};
export declare function useAppData<T = unknown>(): {
    readonly data: T | undefined;
    readonly loading: boolean;
    readonly error: Error | null;
    readonly refresh: () => Promise<T>;
};
export declare function useRecords<T = JsonObject>(entity: string, query?: {
    limit?: number;
    offset?: number;
}): {
    readonly records: T[];
    readonly meta: JsonObject;
    readonly loading: boolean;
    readonly error: Error | null;
    readonly refresh: () => Promise<T[]>;
};
export declare function runAction<T = unknown>(operation: string, args?: JsonObject, options?: RequestOptions): Promise<T>;
export {};
