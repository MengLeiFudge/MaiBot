// Run the pinned upstream engine on media already downloaded by the plugin.
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { runStoryboard } from './vendor/dsh-video-storyboard/src/storyboard/pipeline.js';
import { buildEvidence, formatManifest } from './vendor/dsh-video-storyboard/src/storyboard/format.js';

try {
  const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
  const result = await runStoryboard({
    input: options.input,
    from: options.from ?? 0,
    to: options.to,
    transcript: 'auto',
    transcriptFile: options.transcriptFile,
    strategy: 'compact',
    maxSheets: 4,
    frameConcurrency: 2,
    ffmpegPath: options.ffmpeg,
    ffprobePath: options.ffprobe,
    cache: false,
    encode: true,
  });
  const images = [];
  for (const [index, sheet] of result.sheets.entries()) {
    const name = `sheet-${index + 1}.png`;
    await writeFile(join(options.output, name), sheet.png);
    images.push(name);
  }
  // Keep the upstream evidence and per-cell timings, without local paths or
  // the upstream provider-specific token price estimate in the model prompt.
  const evidence = buildEvidence(result);
  evidence.input = options.source;
  result.info.path = 'shared-video';
  const manifest = formatManifest(result, { maxChars: 24000 })
    .replace(/\s+tokens="[^"]*"/g, '')
    .split('\n').filter(line => !/vision tokens|^sheet already in context:|^per-cell times for verification:/.test(line)).join('\n');
  await writeFile(join(options.output, 'result.json'), JSON.stringify({
    images, manifest, evidence, duration: result.info.duration,
    transcript: result.transcript?.source ?? 'none',
    window: result.window,
  }));
} catch (error) {
  process.stderr.write(String(error?.stack ?? error));
  process.exitCode = 1;
}
