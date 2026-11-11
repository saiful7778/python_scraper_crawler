import { Link } from "@inertiajs/react";
import { DashboardShell } from "@/components/dashboard-shell";
import {
  DashboardShellHeader,
  DashboardShellTitle,
} from "@/components/dashboard-shell/DashboardShellHeader";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ArrowLeftIcon } from "@phosphor-icons/react";

export interface CrawlPage {
  url: string;
  depth: number;
  status: string;
  http_status: number | null;
  title: string | null;
  final_url: string | null;
  skip_reason: string | null;
  error_message: string | null;
}

export interface CrawlRun {
  id: string;
  requested_url: string;
  status: string;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  pages: CrawlPage[];
}

export interface CrawlFinding {
  group: string;
  key: string;
  value: string;
}

export interface CrawlWebsite {
  id: string;
  host: string;
  registrable_domain: string;
  origin: string;
  status: string;
  error_code: string | null;
  error_message: string | null;
  finished_at: string | null;
  tech: Record<string, string | null>;
  runs: CrawlRun[];
  findings: CrawlFinding[];
}

export interface LeadDetail {
  id: string;
  counter: number | null;
  title: string | null;
  categories: string[];
  address: string | null;
  phone: string | null;
  website: string | null;
  website_domain: string | null;
  status: string | null;
  descriptions: string | null;
  owner: string | null;
  timezone: string | null;
  created_at: string;
  updated_at: string;
  emails: string[];
  socials: { platform: string; url: string }[];
  detailed_address: {
    complete_address: string;
    borough: string | null;
    street: string | null;
    city: string | null;
    state: string | null;
    postal_code: string | null;
    country: string | null;
    latitude: number | null;
    longitude: number | null;
    plus_code: string | null;
  } | null;
  crawl_websites: CrawlWebsite[];
}

interface LeadDetailsProps {
  lead: LeadDetail;
}

function Field({
  label,
  value,
}: {
  label: string;
  value: string | number | null;
}) {
  return (
    <div className="space-y-1">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="wrap-break-word">{value ?? "—"}</dd>
    </div>
  );
}

function CrawlWebsiteCard({ website }: { website: CrawlWebsite }) {
  const techEntries = Object.entries(website.tech).filter(([, value]) => value);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          {website.host}
          <Badge variant="outline">{website.status}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="Origin" value={website.origin} />
          <Field label="Registrable domain" value={website.registrable_domain} />
          <Field label="Finished" value={website.finished_at} />
          <Field label="Error code" value={website.error_code} />
          <Field label="Error message" value={website.error_message} />
        </dl>

        <div className="space-y-2">
          <h4 className="font-medium">Technologies & tags</h4>
          {techEntries.length === 0 ? (
            <p className="text-sm text-muted-foreground">None detected.</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {techEntries.map(([name, value]) => (
                <Badge key={name} variant="secondary">
                  {name.replaceAll("_", " ")}: {value}
                </Badge>
              ))}
            </div>
          )}
        </div>

        {website.findings.length > 0 && (
          <div className="space-y-2">
            <h4 className="font-medium">Findings</h4>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Group</TableHead>
                  <TableHead>Key</TableHead>
                  <TableHead>Value</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {website.findings.map((finding, index) => (
                  <TableRow key={`${finding.group}-${finding.key}-${index}`}>
                    <TableCell>{finding.group}</TableCell>
                    <TableCell>{finding.key}</TableCell>
                    <TableCell className="break-all whitespace-normal">
                      {finding.value}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}

        {website.runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">No crawl runs.</p>
        ) : (
          website.runs.map((run) => (
            <div key={run.id} className="space-y-2">
              <h4 className="flex flex-wrap items-center gap-2 font-medium">
                Run: {run.requested_url}
                <Badge variant="outline">{run.status}</Badge>
              </h4>
              <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <Field label="Started" value={run.started_at} />
                <Field label="Finished" value={run.finished_at} />
                <Field label="Error code" value={run.error_code} />
                <Field label="Error message" value={run.error_message} />
              </dl>
              {run.pages.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No pages crawled.
                </p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Depth</TableHead>
                      <TableHead>URL</TableHead>
                      <TableHead>Title</TableHead>
                      <TableHead>HTTP</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Note</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {run.pages.map((page, index) => (
                      <TableRow key={`${page.url}-${index}`}>
                        <TableCell>{page.depth}</TableCell>
                        <TableCell className="break-all whitespace-normal">
                          {page.url}
                        </TableCell>
                        <TableCell className="whitespace-normal">
                          {page.title ?? "—"}
                        </TableCell>
                        <TableCell>{page.http_status ?? "—"}</TableCell>
                        <TableCell>{page.status}</TableCell>
                        <TableCell className="whitespace-normal">
                          {page.skip_reason ?? page.error_message ?? "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

export default function LeadDetails({ lead }: LeadDetailsProps) {
  const address = lead.detailed_address;

  return (
    <DashboardShell
      header={
        <DashboardShellHeader>
          <Button
            className="w-fit"
            variant="ghost"
            render={<Link href="/leads" />}
          >
            <ArrowLeftIcon size={32} />
            <span>Back to all leads</span>
          </Button>
          <DashboardShellTitle>
            {lead.title ?? "Lead details"}
          </DashboardShellTitle>
        </DashboardShellHeader>
      }
    >
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>Overview</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <Field label="#" value={lead.counter} />
              <Field label="Status" value={lead.status} />
              <Field label="Owner" value={lead.owner} />
              <Field label="Phone" value={lead.phone} />
              <Field label="Timezone" value={lead.timezone} />
              <Field label="Website" value={lead.website} />
              <Field label="Website domain" value={lead.website_domain} />
              <Field label="Address" value={lead.address} />
              <Field label="Created" value={lead.created_at} />
              <Field label="Updated" value={lead.updated_at} />
            </dl>
            {lead.descriptions && (
              <p className="mt-4 text-sm whitespace-pre-line">
                {lead.descriptions}
              </p>
            )}
          </CardContent>
        </Card>

        <div className="grid gap-4 md:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Emails</CardTitle>
            </CardHeader>
            <CardContent>
              {lead.emails.length === 0 ? (
                <p className="text-sm text-muted-foreground">No emails.</p>
              ) : (
                <ul className="space-y-2">
                  {lead.emails.map((email) => (
                    <li key={email} className="break-all">
                      <a href={`mailto:${email}`} className="hover:underline">
                        {email}
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Categories</CardTitle>
            </CardHeader>
            <CardContent>
              {lead.categories.length === 0 ? (
                <p className="text-sm text-muted-foreground">No categories.</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {lead.categories.map((name) => (
                    <Badge key={name} variant="secondary">
                      {name}
                    </Badge>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Social profiles</CardTitle>
            </CardHeader>
            <CardContent>
              {lead.socials.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No social profiles.
                </p>
              ) : (
                <ul className="space-y-2">
                  {lead.socials.map((social) => (
                    <li key={social.url} className="break-all">
                      <span className="mr-2 font-medium capitalize">
                        {social.platform}:
                      </span>
                      <a
                        href={social.url}
                        target="_blank"
                        rel="noreferrer"
                        className="hover:underline"
                      >
                        {social.url}
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Detailed address</CardTitle>
            </CardHeader>
            <CardContent>
              {address === null ? (
                <p className="text-sm text-muted-foreground">
                  No detailed address.
                </p>
              ) : (
                <dl className="grid gap-4 sm:grid-cols-2">
                  <Field
                    label="Complete address"
                    value={address.complete_address}
                  />
                  <Field label="Borough" value={address.borough} />
                  <Field label="Street" value={address.street} />
                  <Field label="City" value={address.city} />
                  <Field label="State" value={address.state} />
                  <Field label="Postal code" value={address.postal_code} />
                  <Field label="Country" value={address.country} />
                  <Field label="Plus code" value={address.plus_code} />
                  <Field label="Latitude" value={address.latitude} />
                  <Field label="Longitude" value={address.longitude} />
                </dl>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <h3 className="text-lg font-semibold">Crawl data</h3>
          {lead.crawl_websites.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No crawl data for this lead.
            </p>
          ) : (
            lead.crawl_websites.map((website) => (
              <CrawlWebsiteCard key={website.id} website={website} />
            ))
          )}
        </div>
      </div>
    </DashboardShell>
  );
}
