import { cn } from "cn";

interface DashboardShellProps extends React.ComponentProps<"div"> {
  header?: React.ReactNode;
}

export function DashboardShell({
  children,
  className,
  header,
  ...props
}: DashboardShellProps) {
  return (
    <div
      data-slot="dashboard-shell"
      className={cn("w-full px-2 pt-2 pb-2 sm:px-4 sm:pt-3 sm:pb-4", className)}
      {...props}
    >
      <div className="mx-auto w-full max-w-7xl space-y-4 md:space-y-6">
        {header && header}
        <div className="space-y-4 md:space-y-6">{children}</div>
      </div>
    </div>
  );
}
