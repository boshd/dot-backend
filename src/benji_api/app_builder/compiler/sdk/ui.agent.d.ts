import React, { type InputHTMLAttributes, type ReactNode } from "react";
export type DotAccent = "coral" | "sage" | "ocean" | "plum" | "sky";
type DotSpaceToken = "xs" | "sm" | "md" | "lg" | "xl";
export type DotSpace = DotSpaceToken | "small" | "medium" | "large";
export type DotSize = "sm" | "md" | "lg" | "small" | "medium" | "large";
type ChildrenProps = {
    children?: ReactNode;
};
type SafeButtonProps = Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "className" | "style" | "color">;
type SafeInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "className" | "style" | "color" | "size">;
type SafeTextareaProps = Omit<React.TextareaHTMLAttributes<HTMLTextAreaElement>, "className" | "style" | "color">;
type SafeSelectProps = Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "className" | "style" | "color" | "size">;
type SafeFormProps = Omit<React.FormHTMLAttributes<HTMLFormElement>, "className" | "style" | "color">;
export type WorkflowOperation = "records.create";
type WorkflowTargetProps = {
    entity: string;
    operation?: WorkflowOperation;
} | {
    entity?: never;
    operation?: never;
};
type SelectOption = {
    value: string | number;
    label: ReactNode;
    disabled?: boolean;
};
type ValueChangeProps = {
    onValueChange?: (value: string) => void;
};
export declare const chartTokens: Readonly<{
    primary: "var(--dot-chart-1)";
    secondary: "var(--dot-chart-2)";
    tertiary: "var(--dot-chart-3)";
    muted: "var(--dot-chart-muted)";
    grid: "var(--dot-chart-grid)";
    surface: "var(--dot-surface)";
    ink: "var(--dot-ink)";
}>;
export declare function AppShell({ title, description, eyebrow, accent, width, density, children, }: ChildrenProps & {
    title?: ReactNode;
    description?: ReactNode;
    eyebrow?: ReactNode;
    accent?: DotAccent;
    width?: "compact" | "standard" | "wide";
    density?: "compact" | "comfortable";
}): import("react/jsx-runtime").JSX.Element;
export declare function Section({ eyebrow, title, description, action, tone, children, }: ChildrenProps & {
    eyebrow?: ReactNode;
    title?: ReactNode;
    description?: ReactNode;
    action?: ReactNode;
    tone?: "plain" | "surface" | "soft";
}): import("react/jsx-runtime").JSX.Element;
export declare function Stack({ gap, align, children, }: ChildrenProps & {
    gap?: DotSpace;
    align?: "start" | "center" | "end" | "stretch";
}): import("react/jsx-runtime").JSX.Element;
export declare function Cluster({ children, justify, align, gap, }: ChildrenProps & {
    justify?: "start" | "center" | "end" | "between";
    align?: "start" | "center" | "end" | "stretch";
    gap?: DotSpace;
}): import("react/jsx-runtime").JSX.Element;
export declare function Grid({ columns, gap, children, }: ChildrenProps & {
    columns?: 1 | 2 | 3 | 4;
    gap?: DotSize;
}): import("react/jsx-runtime").JSX.Element;
export declare function Card({ children, tone, padding, }: ChildrenProps & {
    tone?: "default" | "soft" | "accent" | "dark" | "plain";
    padding?: "none" | DotSize;
}): import("react/jsx-runtime").JSX.Element;
export declare function Button({ children, variant, size, type, ...props }: SafeButtonProps & {
    variant?: "primary" | "accent" | "secondary" | "ghost" | "danger";
    size?: DotSize;
}): import("react/jsx-runtime").JSX.Element;
export declare function WorkflowForm({ entity, operation, gap, children, ...props }: Omit<SafeFormProps, "onSubmit"> & ChildrenProps & {
    entity: string;
    operation?: WorkflowOperation;
    gap?: DotSpace;
    onSubmit: NonNullable<SafeFormProps["onSubmit"]>;
}): import("react/jsx-runtime").JSX.Element;
export declare function PrimaryWorkflowTrigger({ children, variant, size, entity, operation, ...props }: Omit<SafeButtonProps, "type"> & WorkflowTargetProps & {
    onClick: NonNullable<SafeButtonProps["onClick"]>;
    variant?: "primary" | "accent" | "secondary";
    size?: DotSize;
}): import("react/jsx-runtime").JSX.Element;
export declare function IconButton({ label, children, variant, ...props }: SafeButtonProps & {
    label: string;
    variant?: "primary" | "accent" | "secondary" | "ghost" | "danger";
}): import("react/jsx-runtime").JSX.Element;
export declare function Badge({ children, tone, }: ChildrenProps & {
    tone?: "neutral" | "accent" | "success" | "warning" | "danger";
}): import("react/jsx-runtime").JSX.Element;
export declare function Heading({ children, level, size, }: ChildrenProps & {
    level?: 1 | 2 | 3 | 4;
    size?: DotSize;
}): import("react/jsx-runtime").JSX.Element;
export declare function Text({ children, tone, size, }: ChildrenProps & {
    tone?: "default" | "muted" | "success" | "danger";
    size?: DotSize;
}): import("react/jsx-runtime").JSX.Element;
export declare function Metric({ label, value, detail, trend, }: {
    label: ReactNode;
    value: ReactNode;
    detail?: ReactNode;
    trend?: "up" | "down" | "neutral";
}): import("react/jsx-runtime").JSX.Element;
export declare function Progress({ value, max, label, }: {
    value: number;
    max?: number;
    label?: ReactNode;
}): import("react/jsx-runtime").JSX.Element;
export declare function Divider(): import("react/jsx-runtime").JSX.Element;
export declare function Callout({ title, action, children, tone, }: ChildrenProps & {
    title?: ReactNode;
    action?: ReactNode;
    tone?: "neutral" | "accent" | "success" | "warning" | "danger";
}): import("react/jsx-runtime").JSX.Element;
export declare function SegmentedControl({ label, children, value, onChange, onValueChange, }: ChildrenProps & {
    label: string;
    value?: string;
    onChange?: (value: string) => void;
    onValueChange?: (value: string) => void;
}): import("react/jsx-runtime").JSX.Element;
export declare function Segment({ active, value, children, onClick, ...props }: Omit<SafeButtonProps, "value"> & {
    active?: boolean;
    value?: string;
}): import("react/jsx-runtime").JSX.Element;
export declare function Field({ label, hint, error, children, }: ChildrenProps & {
    label?: ReactNode;
    hint?: ReactNode;
    error?: ReactNode;
}): import("react/jsx-runtime").JSX.Element;
type LabeledControlProps = {
    label?: ReactNode;
    hint?: ReactNode;
    error?: ReactNode;
};
export declare function Input({ label, hint, error, onChange, onValueChange, ...props }: SafeInputProps & LabeledControlProps & ValueChangeProps): import("react/jsx-runtime").JSX.Element;
export declare function Textarea({ label, hint, error, onChange, onValueChange, ...props }: SafeTextareaProps & LabeledControlProps & ValueChangeProps): import("react/jsx-runtime").JSX.Element;
export declare function Select({ label, hint, error, options, children, onChange, onValueChange, ...props }: SafeSelectProps & LabeledControlProps & ValueChangeProps & {
    options?: readonly SelectOption[];
}): import("react/jsx-runtime").JSX.Element;
export declare function Checkbox({ label, onChange, onCheckedChange, onValueChange, ...props }: SafeInputProps & {
    label?: ReactNode;
    onCheckedChange?: (checked: boolean) => void;
    onValueChange?: (checked: boolean) => void;
}): import("react/jsx-runtime").JSX.Element;
export declare function List({ children, divided }: ChildrenProps & {
    divided?: boolean;
}): import("react/jsx-runtime").JSX.Element;
export declare function ListItem({ leading, title, detail, meta, action, children, }: {
    leading?: ReactNode;
    title?: ReactNode;
    detail?: ReactNode;
    meta?: ReactNode;
    action?: ReactNode;
    children?: ReactNode;
}): import("react/jsx-runtime").JSX.Element;
export declare const Item: typeof ListItem;
export declare function Tabs({ value, onChange, onValueChange, children, }: ChildrenProps & {
    value?: string;
    onChange?: (value: string) => void;
    onValueChange?: (value: string) => void;
}): import("react/jsx-runtime").JSX.Element;
export declare function TabsList({ children }: ChildrenProps): import("react/jsx-runtime").JSX.Element;
export declare function TabsTrigger({ value, children, onClick, ...props }: Omit<SafeButtonProps, "value"> & {
    value: string;
}): import("react/jsx-runtime").JSX.Element;
export declare function TabsContent({ value, children }: ChildrenProps & {
    value: string;
}): import("react/jsx-runtime").JSX.Element | null;
export declare function EmptyState({ title, description, action, }: {
    title: ReactNode;
    description?: ReactNode;
    action?: ReactNode;
}): import("react/jsx-runtime").JSX.Element;
export {};
