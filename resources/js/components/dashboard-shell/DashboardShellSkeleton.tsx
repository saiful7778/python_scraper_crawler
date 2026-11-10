import { cn } from "cn";

interface DashboardShellSkeletonProps extends React.ComponentProps<"div"> {
  header?: React.ReactNode;
}

export function DashboardShellSkeleton({
  children,
  className,
  header,
  ...props
}: DashboardShellSkeletonProps) {
  return (
    <div
      data-slot="dashboard-shell-skeleton"
      className={cn("w-full px-2 pt-2 pb-2 sm:px-4 sm:pt-3 sm:pb-4", className)}
      aria-hidden="true"
      {...props}
    >
      <div className="mx-auto w-full max-w-7xl space-y-4 md:space-y-6">
        {header && header}
        {children && <div className="space-y-4 md:space-y-6">{children}</div>}
      </div>
    </div>
  );
}
