// Run with the pinned Renovate image used by renovate-policy.yml.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const { extractPackageFile } = await import('/usr/local/renovate/dist/modules/manager/custom/regex/index.js');
const raw = JSON.parse(await fs.readFile('renovate.json', 'utf8'));
const manager = raw.customManagers.find(m => m.description.includes('Gitea'));
const source = await fs.readFile('build-images.sh', 'utf8');
const tag = source.match(/gitea_image="docker\.gitea\.com\/gitea:([^"@]+)(?:@sha256:[a-fA-F0-9]{64})?"/)[1];
for (const digest of [undefined, 'sha256:' + 'a'.repeat(64)]) {
  const fixture = source.replace(/gitea_image="[^"]+"/, `gitea_image="docker.gitea.com/gitea:${tag}${digest ? '@' + digest : ''}"`);
  const result = await extractPackageFile(fixture, 'build-images.sh', manager);
  assert.equal(result?.deps.length, 1);
  assert.equal(result.deps[0].depName, 'docker.gitea.com/gitea');
  assert.equal(result.deps[0].currentValue, tag);
  assert.equal(result.deps[0].currentDigest, digest);
  console.log(`PASS native Gitea extraction: ${digest ? 'tag+digest' : 'tag-only'}`);
}
