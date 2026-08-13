import React, { type InputHTMLAttributes, type ReactNode } from "react";

export type DotAccent = "coral" | "sage" | "ocean" | "plum" | "sky";
type DotSpaceToken = "xs" | "sm" | "md" | "lg" | "xl";
export type DotSpace = DotSpaceToken | "small" | "medium" | "large";
export type DotSize = "sm" | "md" | "lg" | "small" | "medium" | "large";

type ChildrenProps = { children?: ReactNode };
type SafeButtonProps = Omit<
  React.ButtonHTMLAttributes<HTMLButtonElement>,
  "className" | "style" | "color"
>;
type SafeInputProps = Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "className" | "style" | "color" | "size"
>;
type SafeTextareaProps = Omit<
  React.TextareaHTMLAttributes<HTMLTextAreaElement>,
  "className" | "style" | "color"
>;
type SafeSelectProps = Omit<
  React.SelectHTMLAttributes<HTMLSelectElement>,
  "className" | "style" | "color" | "size"
>;
type SafeFormProps = Omit<
  React.FormHTMLAttributes<HTMLFormElement>,
  "className" | "style" | "color"
>;
export type WorkflowOperation = "records.create";
type WorkflowTargetProps =
  | { entity: string; operation?: WorkflowOperation }
  | { entity?: never; operation?: never };
type SelectOption = {
  value: string | number;
  label: ReactNode;
  disabled?: boolean;
};
type SegmentedControlContextValue = {
  value?: string;
  onValueChange?: (value: string) => void;
};
type TabsContextValue = {
  value?: string;
  onValueChange?: (value: string) => void;
};

type ValueChangeProps = { onValueChange?: (value: string) => void };

const semanticSize = {
  small: "sm",
  medium: "md",
  large: "lg",
} as const;

function normalizeSpace(value: DotSpace): DotSpaceToken {
  return value in semanticSize
    ? semanticSize[value as keyof typeof semanticSize]
    : value as DotSpaceToken;
}

function normalizeSize(value: DotSize): "sm" | "md" | "lg" {
  return value in semanticSize
    ? semanticSize[value as keyof typeof semanticSize]
    : value as "sm" | "md" | "lg";
}

const SegmentedControlContext = React.createContext<SegmentedControlContextValue | null>(null);
const TabsContext = React.createContext<TabsContextValue | null>(null);
const ItemLeadingContext = React.createContext(false);

export const chartTokens = Object.freeze({
  primary: "var(--dot-chart-1)",
  secondary: "var(--dot-chart-2)",
  tertiary: "var(--dot-chart-3)",
  muted: "var(--dot-chart-muted)",
  grid: "var(--dot-chart-grid)",
  surface: "var(--dot-surface)",
  ink: "var(--dot-ink)",
});

export function AppShell({
  title,
  description,
  eyebrow,
  accent = "coral",
  width = "standard",
  density = "compact",
  children,
}: ChildrenProps & {
  title?: ReactNode;
  description?: ReactNode;
  eyebrow?: ReactNode;
  accent?: DotAccent;
  width?: "compact" | "standard" | "wide";
  density?: "compact" | "comfortable";
}) {
  return (
    <main className="dot-app-shell" data-accent={accent} data-width={width} data-density={density}>
      {(eyebrow || title || description) && (
        <header className="dot-app-header">
          {eyebrow && <span className="dot-overline">{eyebrow}</span>}
          {title && <h1>{title}</h1>}
          {description && <p>{description}</p>}
        </header>
      )}
      {children}
    </main>
  );
}

export function Section({
  eyebrow,
  title,
  description,
  action,
  tone = "plain",
  children,
}: ChildrenProps & {
  eyebrow?: ReactNode;
  title?: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  tone?: "plain" | "surface" | "soft";
}) {
  return (
    <section className="dot-section" data-tone={tone}>
      {(eyebrow || title || description || action) && (
        <header className="dot-section-header">
          <div className="dot-stack dot-gap-xs">
            {eyebrow && <span className="dot-overline">{eyebrow}</span>}
            {title && <h2 className="dot-section-title">{title}</h2>}
            {description && <p className="dot-section-description">{description}</p>}
          </div>
          {action && <div className="dot-section-action">{action}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stack({
  gap = "md",
  align = "stretch",
  children,
}: ChildrenProps & {
  gap?: DotSpace;
  align?: "start" | "center" | "end" | "stretch";
}) {
  return (
    <div className="dot-stack" data-gap={normalizeSpace(gap)} data-align={align}>
      {children}
    </div>
  );
}

export function Cluster({
  children,
  justify = "start",
  align = "center",
  gap = "sm",
}: ChildrenProps & {
  justify?: "start" | "center" | "end" | "between";
  align?: "start" | "center" | "end" | "stretch";
  gap?: DotSpace;
}) {
  return (
    <div className="dot-cluster" data-justify={justify} data-align={align} data-gap={normalizeSpace(gap)}>
      {children}
    </div>
  );
}

export function Grid({
  columns = 2,
  gap = "md",
  children,
}: ChildrenProps & { columns?: 1 | 2 | 3 | 4; gap?: DotSize }) {
  return (
    <div className="dot-grid" data-columns={columns} data-gap={normalizeSize(gap)}>
      {children}
    </div>
  );
}

export function Card({
  children,
  tone = "default",
  padding = "md",
}: ChildrenProps & {
  tone?: "default" | "soft" | "accent" | "dark" | "plain";
  padding?: "none" | DotSize;
}) {
  return (
    <section className="dot-card" data-tone={tone} data-padding={padding === "none" ? padding : normalizeSize(padding)}>
      {children}
    </section>
  );
}

export function Button({
  children,
  variant = "primary",
  size = "md",
  type = "button",
  ...props
}: SafeButtonProps & {
  variant?: "primary" | "accent" | "secondary" | "ghost" | "danger";
  size?: DotSize;
}) {
  return (
    <button
      {...props}
      type={type}
      className="dot-button"
      data-variant={variant}
      data-size={normalizeSize(size)}
    >
      {children}
    </button>
  );
}

export function WorkflowForm({
  entity,
  operation = "records.create",
  gap = "md",
  children,
  ...props
}: Omit<SafeFormProps, "onSubmit"> & ChildrenProps & {
  entity: string;
  operation?: WorkflowOperation;
  gap?: DotSpace;
  onSubmit: NonNullable<SafeFormProps["onSubmit"]>;
}) {
  return (
    <form
      {...props}
      className="dot-stack"
      data-gap={normalizeSpace(gap)}
      data-dot-operation={operation}
      data-dot-entity={entity}
    >
      {children}
    </form>
  );
}

export function PrimaryWorkflowTrigger({
  children,
  variant = "primary",
  size = "md",
  entity,
  operation = "records.create",
  ...props
}: Omit<SafeButtonProps, "type"> & WorkflowTargetProps & {
  onClick: NonNullable<SafeButtonProps["onClick"]>;
  variant?: "primary" | "accent" | "secondary";
  size?: DotSize;
}) {
  return (
    <button
      {...props}
      type="button"
      className="dot-button"
      data-variant={variant}
      data-size={normalizeSize(size)}
      data-dot-primary-action
      data-dot-operation={entity ? operation : undefined}
      data-dot-entity={entity}
    >
      {children}
    </button>
  );
}

export function IconButton({
  label,
  children,
  variant = "secondary",
  ...props
}: SafeButtonProps & {
  label: string;
  variant?: "primary" | "accent" | "secondary" | "ghost" | "danger";
}) {
  return (
    <button
      {...props}
      type="button"
      aria-label={label}
      className="dot-icon-button"
      data-variant={variant}
    >
      {children}
    </button>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: ChildrenProps & {
  tone?: "neutral" | "accent" | "success" | "warning" | "danger";
}) {
  return <span className="dot-badge" data-tone={tone}>{children}</span>;
}

export function Heading({
  children,
  level = 2,
  size,
}: ChildrenProps & {
  level?: 1 | 2 | 3 | 4;
  size?: DotSize;
}) {
  const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4";
  return <Tag className="dot-heading" data-size={size ? normalizeSize(size) : undefined}>{children}</Tag>;
}

export function Text({
  children,
  tone = "default",
  size = "md",
}: ChildrenProps & {
  tone?: "default" | "muted" | "success" | "danger";
  size?: DotSize;
}) {
  return <p className="dot-text" data-tone={tone} data-size={normalizeSize(size)}>{children}</p>;
}

export function Metric({
  label,
  value,
  detail,
  trend,
}: {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  trend?: "up" | "down" | "neutral";
}) {
  return (
    <div className="dot-metric">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small data-trend={trend}>{detail}</small>}
    </div>
  );
}

export function Progress({
  value,
  max = 100,
  label,
}: {
  value: number;
  max?: number;
  label?: ReactNode;
}) {
  const safeMax = Math.max(max, 1);
  const safeValue = Math.max(0, Math.min(value, safeMax));
  const percent = Math.round((safeValue / safeMax) * 100);
  return (
    <div className="dot-progress-wrap">
      {label && <div className="dot-progress-label"><span>{label}</span><span>{percent}%</span></div>}
      <progress className="dot-progress" max={safeMax} value={safeValue} aria-label={typeof label === "string" ? label : undefined} />
    </div>
  );
}

export function Divider() {
  return <hr className="dot-divider" />;
}

export function Callout({
  title,
  action,
  children,
  tone = "neutral",
}: ChildrenProps & {
  title?: ReactNode;
  action?: ReactNode;
  tone?: "neutral" | "accent" | "success" | "warning" | "danger";
}) {
  return (
    <aside className="dot-callout" data-tone={tone} role={tone === "danger" ? "alert" : undefined}>
      {(title || action) && (
        <div className="dot-callout-header">
          {title && <strong>{title}</strong>}
          {action && <span className="dot-callout-action">{action}</span>}
        </div>
      )}
      {children && <div className="dot-callout-body">{children}</div>}
    </aside>
  );
}

export function SegmentedControl({
  label,
  children,
  value,
  onChange,
  onValueChange,
}: ChildrenProps & {
  label: string;
  value?: string;
  onChange?: (value: string) => void;
  onValueChange?: (value: string) => void;
}) {
  return (
    <SegmentedControlContext.Provider
      value={{ value, onValueChange: onValueChange ?? onChange }}
    >
      <div className="dot-segmented" role="group" aria-label={label}>{children}</div>
    </SegmentedControlContext.Provider>
  );
}

export function Segment({
  active,
  value,
  children,
  onClick,
  ...props
}: Omit<SafeButtonProps, "value"> & { active?: boolean; value?: string }) {
  const control = React.useContext(SegmentedControlContext);
  const selected = active ?? (value !== undefined && control?.value === value);
  return (
    <button
      {...props}
      type="button"
      value={value}
      className="dot-segment"
      aria-pressed={selected}
      onClick={(event) => {
        onClick?.(event);
        if (!event.defaultPrevented && value !== undefined) control?.onValueChange?.(value);
      }}
    >
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  error,
  children,
}: ChildrenProps & { label?: ReactNode; hint?: ReactNode; error?: ReactNode }) {
  return (
    <div className="dot-field">
      {label && <span className="dot-field-label">{label}</span>}
      {children}
      {error ? <span className="dot-field-error">{error}</span> : hint && <span className="dot-field-hint">{hint}</span>}
    </div>
  );
}

type LabeledControlProps = { label?: ReactNode; hint?: ReactNode; error?: ReactNode };

export function Input({
  label,
  hint,
  error,
  onChange,
  onValueChange,
  ...props
}: SafeInputProps & LabeledControlProps & ValueChangeProps) {
  const control = <input {...props} className="dot-input" onChange={(event) => {
    onChange?.(event);
    if (!event.defaultPrevented) onValueChange?.(event.currentTarget.value);
  }} />;
  if (!(label || hint || error)) return control;
  return (
    <Field hint={hint} error={error}>
      <label className="dot-control-label">
        {label && <span className="dot-field-label">{label}</span>}
        {control}
      </label>
    </Field>
  );
}

export function Textarea({
  label,
  hint,
  error,
  onChange,
  onValueChange,
  ...props
}: SafeTextareaProps & LabeledControlProps & ValueChangeProps) {
  const control = <textarea {...props} className="dot-input dot-textarea" onChange={(event) => {
    onChange?.(event);
    if (!event.defaultPrevented) onValueChange?.(event.currentTarget.value);
  }} />;
  if (!(label || hint || error)) return control;
  return (
    <Field hint={hint} error={error}>
      <label className="dot-control-label">
        {label && <span className="dot-field-label">{label}</span>}
        {control}
      </label>
    </Field>
  );
}

export function Select({
  label,
  hint,
  error,
  options,
  children,
  onChange,
  onValueChange,
  ...props
}: SafeSelectProps & LabeledControlProps & ValueChangeProps & { options?: readonly SelectOption[] }) {
  const control = (
    <select {...props} className="dot-input dot-select" onChange={(event) => {
      onChange?.(event);
      if (!event.defaultPrevented) onValueChange?.(event.currentTarget.value);
    }}>
      {children ?? options?.map((option, index) => (
        <option
          key={`${String(option.value)}:${index}`}
          value={option.value}
          disabled={option.disabled}
        >
          {option.label}
        </option>
      ))}
    </select>
  );
  if (!(label || hint || error)) return control;
  return (
    <Field hint={hint} error={error}>
      <label className="dot-control-label">
        {label && <span className="dot-field-label">{label}</span>}
        {control}
      </label>
    </Field>
  );
}

export function Checkbox({
  label,
  onChange,
  onCheckedChange,
  onValueChange,
  ...props
}: SafeInputProps & {
  label?: ReactNode;
  onCheckedChange?: (checked: boolean) => void;
  onValueChange?: (checked: boolean) => void;
}) {
  const inLeading = React.useContext(ItemLeadingContext);
  const accessible = inLeading && typeof label === "string" ? label : undefined;
  return (
    <label className="dot-checkbox" data-compact={inLeading ? "true" : undefined}>
      <input
        {...props}
        type="checkbox"
        aria-label={accessible}
        onChange={(event) => {
          onChange?.(event);
          if (!event.defaultPrevented) {
            onCheckedChange?.(event.currentTarget.checked);
            onValueChange?.(event.currentTarget.checked);
          }
        }}
      />
      {label && <span className={inLeading ? "dot-sr-only" : undefined}>{label}</span>}
    </label>
  );
}

export function List({ children, divided = true }: ChildrenProps & { divided?: boolean }) {
  return <ul className="dot-list" data-divided={divided}>{children}</ul>;
}

export function ListItem({
  leading,
  title,
  detail,
  meta,
  action,
  children,
}: {
  leading?: ReactNode;
  title?: ReactNode;
  detail?: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
}) {
  const copy = title !== undefined
    ? <><strong>{title}</strong>{detail && <span>{detail}</span>}{children}</>
    : children ?? (detail && <span>{detail}</span>);
  return (
    <li className="dot-list-item">
      {leading && (
        <ItemLeadingContext.Provider value={true}>
          <div className="dot-list-leading">{leading}</div>
        </ItemLeadingContext.Provider>
      )}
      <div className="dot-list-copy">{copy}</div>
      {meta && <span className="dot-list-meta">{meta}</span>}
      {action && <div className="dot-list-action">{action}</div>}
    </li>
  );
}

export const Item = ListItem;

export function Tabs({
  value,
  onChange,
  onValueChange,
  children,
}: ChildrenProps & {
  value?: string;
  onChange?: (value: string) => void;
  onValueChange?: (value: string) => void;
}) {
  return (
    <TabsContext.Provider value={{ value, onValueChange: onValueChange ?? onChange }}>
      <div className="dot-tabs">{children}</div>
    </TabsContext.Provider>
  );
}

export function TabsList({ children }: ChildrenProps) {
  return <div className="dot-tabs-list" role="tablist">{children}</div>;
}

export function TabsTrigger({
  value,
  children,
  onClick,
  ...props
}: Omit<SafeButtonProps, "value"> & { value: string }) {
  const tabs = React.useContext(TabsContext);
  const selected = tabs?.value === value;
  return (
    <button
      {...props}
      type="button"
      role="tab"
      aria-selected={selected}
      className="dot-tabs-trigger"
      onClick={(event) => {
        onClick?.(event);
        if (!event.defaultPrevented) tabs?.onValueChange?.(value);
      }}
    >
      {children}
    </button>
  );
}

export function TabsContent({ value, children }: ChildrenProps & { value: string }) {
  const tabs = React.useContext(TabsContext);
  if (tabs?.value !== undefined && tabs.value !== value) return null;
  return <div className="dot-tabs-content" role="tabpanel">{children}</div>;
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return <div className="dot-empty"><h3>{title}</h3>{description && <p>{description}</p>}{action}</div>;
}
