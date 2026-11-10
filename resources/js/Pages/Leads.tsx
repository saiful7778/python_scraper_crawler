import { Link } from "@inertiajs/react";
import { DashboardShell } from "@/components/dashboard-shell";
import {
  DashboardShellHeader,
  DashboardShellTitle,
} from "@/components/dashboard-shell/DashboardShellHeader";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export interface Lead {
  id: string;
  counter: number | null;
  title: string | null;
  category: string | null;
  address: string | null;
  phone: string | null;
  email: string | null;
  website: string | null;
  status: string | null;
}

interface LeadsProps {
  leads: Lead[];
}

export default function Leads({ leads }: LeadsProps) {
  return (
    <DashboardShell
      header={
        <DashboardShellHeader>
          <DashboardShellTitle>All Leads</DashboardShellTitle>
        </DashboardShellHeader>
      }
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>#</TableHead>
            <TableHead>Title</TableHead>
            <TableHead>Category</TableHead>
            <TableHead>Website</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {leads.map((lead, index) => (
            <TableRow key={lead.counter ?? index}>
              <TableCell>{lead.counter ?? "—"}</TableCell>
              <TableCell>
                <div>
                  <Link href={`/leads/${lead.id}`} className="hover:underline">
                    {lead.title ?? "—"}
                  </Link>
                  {lead.phone && (
                    <div className="text-muted-foreground">{lead.phone}</div>
                  )}
                  {lead.email && (
                    <div className="text-muted-foreground">{lead.email}</div>
                  )}
                </div>
              </TableCell>
              <TableCell>{lead.category ?? "—"}</TableCell>
              <TableCell>
                {lead.website ? (
                  <a
                    href={lead.website}
                    target="_blank"
                    className="hover:underline"
                  >
                    {lead.website}
                  </a>
                ) : (
                  "—"
                )}
              </TableCell>
            </TableRow>
          ))}
          {leads.length === 0 && (
            <TableRow>
              <TableCell
                colSpan={7}
                className="text-center text-muted-foreground"
              >
                No leads found.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </DashboardShell>
  );
}
