import { ThemeProvider } from "@/components/providers/theme-provider";
import { AppSidebar } from "@/components/shared/AppSidebar";
import { Topbar } from "@/components/shared/Topbar";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

export default function DefaultLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <ThemeProvider defaultTheme="system">
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset>
          <Topbar />
          <main className="min-h-[calc(100vh-125px)] flex-1">{children}</main>
        </SidebarInset>
      </SidebarProvider>
    </ThemeProvider>
  );
}
