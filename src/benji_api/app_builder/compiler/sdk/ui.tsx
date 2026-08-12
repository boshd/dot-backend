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
type SelectOption = {
  value: string | number;
  label: ReactNode;
  disabled?: boolean;
};
type SegmentedControlContextValue = {
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
  children,
}: ChildrenProps & {
  title?: ReactNode;
  description?: ReactNode;
  eyebrow?: ReactNode;
  accent?: DotAccent;
  width?: "compact" | "standard" | "wide";
}) {
  return (
    <main className="dot-app-shell" data-accent={accent} data-width={width}>
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

export function PrimaryWorkflowTrigger({
  children,
  variant = "primary",
  size = "md",
  ...props
}: Omit<SafeButtonProps, "type"> & {
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
    <label className="dot-field">
      {label && <span className="dot-field-label">{label}</span>}
      {children}
      {error ? <span className="dot-field-error">{error}</span> : hint && <span className="dot-field-hint">{hint}</span>}
    </label>
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
  return label || hint || error ? <Field label={label} hint={hint} error={error}>{control}</Field> : control;
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
  return label || hint || error ? <Field label={label} hint={hint} error={error}>{control}</Field> : control;
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
  return label || hint || error ? <Field label={label} hint={hint} error={error}>{control}</Field> : control;
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
  return <label className="dot-checkbox"><input {...props} type="checkbox" onChange={(event) => {
    onChange?.(event);
    if (!event.defaultPrevented) {
      onCheckedChange?.(event.currentTarget.checked);
      onValueChange?.(event.currentTarget.checked);
    }
  }} />{label && <span>{label}</span>}</label>;
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
      {leading && <div className="dot-list-leading">{leading}</div>}
      <div className="dot-list-copy">{copy}</div>
      {meta && <span className="dot-list-meta">{meta}</span>}
      {action && <div className="dot-list-action">{action}</div>}
    </li>
  );
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
