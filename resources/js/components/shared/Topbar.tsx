import { SidebarTrigger } from "@/components/ui/sidebar";

export function Topbar() {
  return (
    <header className="sticky top-0 z-50 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-sidebar-border bg-sidebar px-4 transition-[width,height] ease-linear">
      <div className="flex items-center gap-2">
        <SidebarTrigger />
      </div>
    </header>
  );
}
