import { cn } from "cn";

export function DashboardShellHeader({
  className,
  ...props
}: React.ComponentProps<"h1">) {
  return (
    <div
      data-slot="dashboard-shell-header"
      className={cn(
        "space-y-1 leading-normal has-data-[slot=dashboard-shell-header-active]:grid has-data-[slot=dashboard-shell-header-active]:grid-cols-[1fr_auto] has-data-[slot=dashboard-shell-header-active]:gap-1 has-data-[slot=dashboard-shell-header-active]:space-y-0",
        className
      )}
      {...props}
    />
  );
}

export function DashboardShellHeaderActive({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dashboard-shell-header-active"
      className={cn(
        "col-start-2 row-span-2 row-start-1 self-start justify-self-end",
        className
      )}
      {...props}
    />
  );
}

export function DashboardShellTitle({
  className,
  ...props
}: React.ComponentProps<"h1">) {
  return (
    <h1
      data-slot="dashboard-shell-title"
      className={cn("text-lg font-bold md:text-3xl", className)}
      {...props}
    />
  );
}

export function DashboardShellDescription({
  className,
  ...props
}: React.ComponentProps<"h1">) {
  return (
    <p
      data-slot="dashboard-shell-description"
      className={cn("text-sm text-muted-foreground", className)}
      {...props}
    />
  );
}
