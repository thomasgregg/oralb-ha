'use client';

import {
  Activity,
  BarChart3,
  CalendarDays,
  Check,
  Download,
  ExternalLink,
  GitBranch,
  Info,
  Package,
  RefreshCw,
  Sparkles,
  TrendingUp,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';

type ReleaseMetric = {
  version: string;
  downloads: number;
  publishedAt: string;
  size: number;
  url: string;
};

type GitHubRelease = {
  tag_name: string;
  published_at: string | null;
  html_url: string;
  draft: boolean;
  assets: Array<{
    name: string;
    download_count: number;
    size: number;
  }>;
};

const FALLBACK_RELEASES: ReleaseMetric[] = [
  { version: 'v0.7.29', downloads: 8, publishedAt: '2026-08-21T19:59:28Z', size: 132757, url: 'https://github.com/thomasgregg/oralb-ha/releases/tag/v0.7.29' },
  { version: 'v0.7.28', downloads: 9, publishedAt: '2026-08-21T19:13:28Z', size: 132769, url: 'https://github.com/thomasgregg/oralb-ha/releases/tag/v0.7.28' },
  { version: 'v0.7.27', downloads: 58, publishedAt: '2026-08-21T13:29:19Z', size: 132769, url: 'https://github.com/thomasgregg/oralb-ha/releases/tag/v0.7.27' },
  { version: 'v0.7.26', downloads: 3, publishedAt: '2026-08-21T13:19:23Z', size: 132769, url: 'https://github.com/thomasgregg/oralb-ha/releases/tag/v0.7.26' },
  { version: 'v0.7.24', downloads: 0, publishedAt: '2026-08-21T11:46:44Z', size: 132769, url: 'https://github.com/thomasgregg/oralb-ha/releases/tag/v0.7.24' },
  { version: 'v0.7.23', downloads: 0, publishedAt: '2026-08-21T10:26:11Z', size: 132667, url: 'https://github.com/thomasgregg/oralb-ha/releases/tag/v0.7.23' },
  { version: 'v0.7.22', downloads: 0, publishedAt: '2026-08-21T09:15:17Z', size: 131808, url: 'https://github.com/thomasgregg/oralb-ha/releases/tag/v0.7.22' },
  { version: 'v0.7.21', downloads: 0, publishedAt: '2026-08-21T09:04:35Z', size: 131545, url: 'https://github.com/thomasgregg/oralb-ha/releases/tag/v0.7.21' },
  { version: 'v0.7.20', downloads: 0, publishedAt: '2026-08-20T19:53:04Z', size: 131529, url: 'https://github.com/thomasgregg/oralb-ha/releases/tag/v0.7.20' },
];

const API_URL = 'https://api.github.com/repos/thomasgregg/oralb-ha/releases?per_page=100';

function formatNumber(value: number) {
  return new Intl.NumberFormat('en').format(value);
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('en', {
    day: 'numeric',
    hour: '2-digit',
    hour12: false,
    month: 'short',
    minute: '2-digit',
    timeZone: 'UTC',
    timeZoneName: 'short',
    year: 'numeric',
  }).format(new Date(value));
}

function timeAgo(date: Date | null) {
  if (!date) return 'Using recent snapshot';
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 10) return 'Just now';
  if (seconds < 60) return `${seconds}s ago`;
  return `${Math.floor(seconds / 60)}m ago`;
}

function StatCard({ label, value, note, icon, primary = false }: { label: string; value: string; note: ReactNode; icon: ReactNode; primary?: boolean }) {
  return (
    <article className={`stat-card${primary ? ' stat-primary' : ''}`}>
      <div className="stat-topline">
        <span className="stat-label">{label}</span>
        <span className="stat-icon" aria-hidden="true">{icon}</span>
      </div>
      <strong>{value}</strong>
      <p>{note}</p>
    </article>
  );
}

export default function Home() {
  const [releases, setReleases] = useState(FALLBACK_RELEASES);
  const [status, setStatus] = useState<'loading' | 'ready' | 'stale'>('loading');
  const [refreshState, setRefreshState] = useState<'idle' | 'refreshing' | 'updated' | 'error'>('idle');
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [range, setRange] = useState<'all' | 'recent'>('all');

  const refresh = useCallback(async () => {
    setStatus('loading');
    setRefreshState('refreshing');
    try {
      const response = await fetch(API_URL, {
        cache: 'no-store',
        headers: { Accept: 'application/vnd.github+json' },
      });
      if (!response.ok) throw new Error(`GitHub returned ${response.status}`);
      const payload = await response.json() as GitHubRelease[];
      const metrics = payload.flatMap((release) => {
        const asset = release.assets.find((candidate) => candidate.name === 'oralb_live.zip');
        if (!asset || release.draft || !release.published_at) return [];
        return [{
          version: release.tag_name,
          downloads: asset.download_count,
          publishedAt: release.published_at,
          size: asset.size,
          url: release.html_url,
        }];
      });
      if (metrics.length === 0) throw new Error('No tracked assets found');
      setReleases(metrics);
      setLastUpdated(new Date());
      setStatus('ready');
      setRefreshState('updated');
    } catch {
      setStatus('stale');
      setRefreshState('error');
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 300_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (refreshState !== 'updated') return;
    const timer = window.setTimeout(() => setRefreshState('idle'), 2_400);
    return () => window.clearTimeout(timer);
  }, [refreshState]);

  const summary = useMemo(() => {
    const total = releases.reduce((sum, release) => sum + release.downloads, 0);
    const leader = [...releases].sort((a, b) => b.downloads - a.downloads)[0];
    const activeCount = Math.max(1, releases.filter((release) => release.downloads > 0).length);
    return {
      total,
      latest: releases[0],
      leader,
      leaderShare: total ? Math.round((leader.downloads / total) * 100) : 0,
      average: Math.round(total / activeCount),
    };
  }, [releases]);

  const chartReleases = useMemo(() => {
    const selected = range === 'recent' ? releases.slice(0, 5) : releases;
    return [...selected].reverse();
  }, [range, releases]);
  const maxDownloads = Math.max(1, ...chartReleases.map((release) => release.downloads));

  return (
    <main className="dashboard-shell" id="top">
      <header className="site-header">
        <a className="brand" href="#top" aria-label="Oral-B Live analytics home">
          <span className="brand-mark" aria-hidden="true">OB</span>
          <span>
            <strong>Oral-B Live</strong>
            <small>Integration analytics</small>
          </span>
        </a>
        <div className="header-actions">
          <span className={`live-pill status-${status}`}>
            <i /> {status === 'stale' ? 'Recent snapshot' : status === 'loading' ? 'Connecting…' : 'Live from GitHub'}
          </span>
          <a className="github-button" href="https://github.com/thomasgregg/oralb-ha" target="_blank" rel="noreferrer">
            <GitBranch size={14} aria-hidden="true" /> <span className="github-label">Repository</span> <ExternalLink size={12} aria-hidden="true" />
          </a>
        </div>
      </header>

      <section className="hero">
        <div>
          <p className="eyebrow"><Sparkles size={13} /> Community adoption dashboard</p>
          <h1>Downloads, without<br />the guesswork.</h1>
          <p className="hero-copy">A clear, live view of HACS ZIP downloads across every tracked release of Oral-B Live.</p>
        </div>
        <div className="hero-refresh">
          <span>Last refreshed</span>
          <strong>{timeAgo(lastUpdated)}</strong>
          <button
            type="button"
            className={`refresh-button state-${refreshState}`}
            onClick={() => void refresh()}
            aria-label="Refresh GitHub download data"
            disabled={refreshState === 'refreshing'}
          >
            {refreshState === 'updated'
              ? <Check size={14} aria-hidden="true" />
              : <RefreshCw size={14} className={refreshState === 'refreshing' ? 'is-spinning' : ''} aria-hidden="true" />}
            <span aria-live="polite">
              {refreshState === 'refreshing'
                ? 'Refreshing…'
                : refreshState === 'updated'
                  ? 'Updated'
                  : refreshState === 'error'
                    ? 'Try again'
                    : 'Refresh data'}
            </span>
          </button>
        </div>
      </section>

      <section className="stats-grid" aria-label="Download summary">
        <StatCard primary label="Total downloads" value={formatNumber(summary.total)} icon={<Download size={18} />} note={<>Across {releases.length} tracked releases</>} />
        <StatCard label="Latest release" value={formatNumber(summary.latest.downloads)} icon={<Activity size={18} />} note={<><span className="version-chip">{summary.latest.version}</span> asset downloads</>} />
        <StatCard label="Most downloaded" value={formatNumber(summary.leader.downloads)} icon={<TrendingUp size={18} />} note={<><span className="version-chip">{summary.leader.version}</span> · {summary.leaderShare}% of total</>} />
        <StatCard label="Active-release avg." value={formatNumber(summary.average)} icon={<BarChart3 size={18} />} note={<>Average among downloaded versions</>} />
      </section>

      <section className="analytics-grid">
        <article className="panel chart-card" aria-labelledby="release-performance-title">
          <div className="card-heading">
            <div>
              <p className="eyebrow">Release performance</p>
              <h2 id="release-performance-title">Downloads by version</h2>
            </div>
            <div className="segmented-control" aria-label="Chart range">
              <button className={range === 'all' ? 'active' : ''} onClick={() => setRange('all')} type="button">All</button>
              <button className={range === 'recent' ? 'active' : ''} onClick={() => setRange('recent')} type="button">Recent 5</button>
            </div>
          </div>
          <div className="chart-area">
            <span className="grid-line grid-line-100" />
            <span className="grid-line grid-line-50" />
            <div className="bar-chart" role="img" aria-label="Bar chart showing ZIP downloads by release version">
              {chartReleases.map((release) => (
                <a className="bar-column" href={release.url} target="_blank" rel="noreferrer" key={release.version} aria-label={`${release.version}: ${release.downloads} downloads`}>
                  <span className="bar-value">{release.downloads || '–'}</span>
                  <div className="bar-track">
                    <span style={{ height: `${Math.max((release.downloads / maxDownloads) * 100, release.downloads ? 7 : 0)}%` }} />
                  </div>
                  <span className="bar-label">{release.version.replace('v0.7.', '.')}</span>
                </a>
              ))}
            </div>
          </div>
          <p className="chart-caption">Versions are shown chronologically. Select a bar to open its GitHub release.</p>
        </article>

        <aside className="panel insight-card" aria-labelledby="distribution-title">
          <div className="card-heading compact">
            <div>
              <p className="eyebrow">Distribution</p>
              <h2 id="distribution-title">Download share</h2>
            </div>
            <Package size={18} aria-hidden="true" />
          </div>
          <div className="donut-wrap">
            <div className="donut" style={{ '--share': `${summary.leaderShare * 3.6}deg` } as CSSProperties}>
              <div><strong>{summary.leaderShare}%</strong><span>top version</span></div>
            </div>
          </div>
          <div className="leader-row">
            <span><i /> {summary.leader.version}</span>
            <strong>{formatNumber(summary.leader.downloads)}</strong>
          </div>
          <div className="leader-row secondary">
            <span><i /> Other releases</span>
            <strong>{formatNumber(summary.total - summary.leader.downloads)}</strong>
          </div>
          <div className="insight-note">
            <TrendingUp size={16} />
            <p><strong>{summary.leader.version}</strong> currently drives most tracked download activity.</p>
          </div>
        </aside>
      </section>

      <section className="panel releases-panel" aria-labelledby="release-table-title">
        <div className="card-heading table-heading">
          <div>
            <p className="eyebrow">Detailed breakdown</p>
            <h2 id="release-table-title">Tracked releases</h2>
          </div>
          <span className="asset-pill"><Package size={13} /> oralb_live.zip</span>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr><th>Version</th><th>Published</th><th>Asset size</th><th>Share</th><th className="align-right">Downloads</th><th><span className="sr-only">Open</span></th></tr>
            </thead>
            <tbody>
              {releases.map((release, index) => {
                const share = summary.total ? Math.round((release.downloads / summary.total) * 100) : 0;
                return (
                  <tr key={release.version}>
                    <td><span className="release-version">{release.version}</span>{index === 0 && <span className="latest-tag">Latest</span>}</td>
                    <td><span className="date-cell"><CalendarDays size={14} /> {formatDate(release.publishedAt)}</span></td>
                    <td>{Math.round(release.size / 1024)} KB</td>
                    <td><span className="share-cell"><i><b style={{ width: `${share}%` }} /></i>{share}%</span></td>
                    <td className="align-right"><strong className="download-count">{formatNumber(release.downloads)}</strong></td>
                    <td><a className="row-link" href={release.url} target="_blank" rel="noreferrer" aria-label={`Open ${release.version} release`}><ExternalLink size={14} /></a></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="method-card">
        <Info size={18} aria-hidden="true" />
        <div><strong>What this measures</strong><p>GitHub counts every ZIP asset download, including first installs, upgrades and redownloads. It is a useful adoption signal, but it is not a unique-user count.</p></div>
        <a href="https://docs.github.com/en/rest/releases/assets" target="_blank" rel="noreferrer">Methodology <ExternalLink size={12} /></a>
      </section>

      <footer>
        <span>Oral-B Live analytics</span>
        <span>Data refreshes automatically every 5 minutes</span>
      </footer>
    </main>
  );
}
