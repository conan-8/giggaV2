/** @jsxImportSource @opentui/solid */
/**
 * GIGGA v2 sidebar plugin — renders the pipeline stage flowchart for the
 * newest gigga2 run against the current project, live, below the session's
 * token/context blocks.
 *
 * Data source: ~/.gigga2/runs/<run>/state.json (the derived cache of the
 * run's append-only journal). Polls every 2s; cheap (small file).
 */
import { createSignal, onCleanup } from 'solid-js';
import type { TuiPlugin, TuiPluginModule } from '@opencode-ai/plugin/tui';
import type fs from 'fs';
import type path from 'path';
import type os from 'os';

const RED = '#FF3B30';

const STAGES: Record<string, string[]> = {
  fasttrack: ['INTAKE', 'TRIAGE', 'FASTTRACK', 'INTEGRATE', 'JUDGE', 'APPLY'],
  discovery: ['INTAKE', 'TRIAGE', 'RECON', 'COVERAGE', 'DISCOVERY', 'INTEGRATE', 'JUDGE', 'APPLY'],
  full: ['INTAKE', 'TRIAGE', 'RECON', 'COVERAGE', 'CLARIFY', 'PROMPT_GEN', 'REVIEW', 'EXECUTE', 'INTEGRATE', 'JUDGE', 'APPLY'],
};

// journal phases -> flowchart labels
const PHASE_LABEL: Record<string, string> = {
  INTAKE: 'INTAKE', TRIAGE: 'TRIAGE', FASTTRACK: 'FASTTRACK', RECON: 'RECON',
  COVERAGE_CHECK: 'COVERAGE', DISCOVERY: 'DISCOVERY', CLARIFY: 'CLARIFY',
  PROMPT_GEN: 'PROMPT_GEN', REVIEW: 'REVIEW', EXECUTE: 'EXECUTE',
  INTEGRATE: 'INTEGRATE', JUDGE: 'JUDGE', APPLY: 'APPLY', HALT: 'HALT',
};

interface RunState {
  phase?: string;
  path?: string;
  terminal?: string;
  run_id?: string;
  repo_path?: string;
  request?: string;
  chains?: Record<string, { status?: string }>;
}

interface RunInfo {
  state: RunState;
  mtime: number;
}

function readLatestRun(cwd: string | undefined): RunInfo | null {
  try {
    // require() is intentional: top-level ESM imports of Node built-ins are
    // not supported in the Bun plugin runtime.
    const fsSync = require('fs') as typeof fs;
    const pathSync = require('path') as typeof path;
    const osSync = require('os') as typeof os;
    const runsDir = pathSync.join(osSync.homedir(), '.gigga2', 'runs');
    let best: RunInfo | null = null;
    for (const entry of fsSync.readdirSync(runsDir)) {
      const file = pathSync.join(runsDir, entry, 'state.json');
      try {
        const stat = fsSync.statSync(file);
        const state = JSON.parse(fsSync.readFileSync(file, 'utf8')) as RunState;
        if (!state.phase) continue;
        // prefer the run bound to this project; fall back to the newest overall
        const mine = cwd && state.repo_path &&
          pathSync.resolve(state.repo_path) === pathSync.resolve(cwd);
        const score = stat.mtimeMs + (mine ? 1e12 : 0);
        if (!best || score > best.mtime) best = { state, mtime: score };
      } catch {
        // unreadable/partial entry — skip
      }
    }
    return best;
  } catch {
    return null;
  }
}

function flowFor(state: RunState): { label: string; status: 'done' | 'current' | 'todo' | 'failed' }[] {
  const pathKey = state.path && STAGES[state.path] ? state.path : 'full';
  const labels = STAGES[pathKey];
  const currentLabel = PHASE_LABEL[state.phase ?? ''] ?? state.phase ?? 'INTAKE';
  const currentIdx = labels.indexOf(currentLabel);
  return labels.map((label, i) => {
    let status: 'done' | 'current' | 'todo' | 'failed' = 'todo';
    if (state.terminal === 'DONE') status = 'done';
    else if (state.terminal === 'HALT') {
      if (currentIdx === -1 || i < currentIdx) status = 'done';
      else if (i === currentIdx) status = 'failed';
    } else if (currentIdx !== -1) {
      if (i < currentIdx) status = 'done';
      else if (i === currentIdx) status = 'current';
    }
    return { label, status };
  });
}

const tui: TuiPlugin = async (api) => {
  api.slots.register({
    order: 5,
    slots: {
      sidebar_content(_ctx, _props) {
        const theme = () => api.theme.current;
        const [run, setRun] = createSignal<RunInfo | null>(null);
        const refresh = () => setRun(readLatestRun(api.state.path.directory));
        refresh();
        const timer = setInterval(refresh, 2000);
        onCleanup(() => clearInterval(timer));

        return (
          <box flexDirection="column" visible={!!run()}>
            <text fg={RED}>
              <b>GIGGA</b>
              <span style={{ fg: theme().textMuted }}>{` ${run()?.state.run_id ?? ''}`}</span>
            </text>
            {(() => {
              const state = run()?.state;
              if (!state) return null;
              const rows = flowFor(state).map((s) => {
                const mark = s.status === 'done' ? '✓' : s.status === 'current' ? '▶' : s.status === 'failed' ? '✗' : '○';
                const fg = s.status === 'done' ? theme().success
                  : s.status === 'current' ? RED
                  : s.status === 'failed' ? theme().error
                  : theme().textMuted;
                const suffix = s.status === 'current' && state.phase === 'CLARIFY' ? ' — waiting on you'
                  : s.status === 'current' && state.phase === 'EXECUTE' ? chainSuffix(state)
                  : '';
                return (
                  <text fg={fg}>
                    {`${mark} ${s.label}`}
                    <span style={{ fg: theme().textMuted }}>{suffix}</span>
                  </text>
                );
              });
              if (state.terminal) {
                rows.push(
                  <text fg={state.terminal === 'DONE' ? theme().success : theme().error}>
                    <b>{state.terminal === 'DONE' ? '● DONE — branch ready' : '● HALT — see report.md'}</b>
                  </text>,
                );
              }
              return rows;
            })()}
          </box>
        );
      },
    },
  });
};

function chainSuffix(state: RunState): string {
  const chains = Object.entries(state.chains ?? {});
  if (!chains.length) return '';
  const parts = chains
    .filter(([id]) => id !== 'result')
    .map(([id, c]) => `${id}:${c.status ?? '?'}`);
  return ` [${parts.join(' ')}]`;
}

const plugin: TuiPluginModule & { id: string } = { id: 'gigga-flow', tui };

export default plugin;
