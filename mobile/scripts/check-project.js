#!/usr/bin/env node
/*
 * Reads project.pbxproj back and checks that Xcode will find what Orenth
 * added: the project parses, every reference in it points at something that
 * exists, Orenth's own Swift files (the screen, the sign-in sheet and the
 * in-app purchase plugin) are compiled by the App target, the privacy
 * manifest is copied into the app, and the app is iPhone-only.
 *
 *   cd mobile && node scripts/check-project.js
 *
 * add-native-files.js runs the same check on what it is about to write.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const parser = require('xcode/lib/parser/pbxproj');

const MOBILE = path.resolve(__dirname, '..');
const PBXPROJ = path.join(MOBILE, 'ios', 'App', 'App.xcodeproj', 'project.pbxproj');
const APP_DIR = path.join(MOBILE, 'ios', 'App', 'App');

const SOURCES = ['AppDelegate.swift', 'SceneDelegate.swift',
                 'MainViewController.swift', 'WebAuthPlugin.swift',
                 'AppPurchasePlugin.swift'];
const RESOURCES = ['PrivacyInfo.xcprivacy'];
// Made by "npx cap sync ios" and kept out of git, so a fresh copy lacks them.
const GENERATED = new Set(['public', 'capacitor.config.json', 'config.xml']);

function unquote(value) {
  return typeof value === 'string' ? value.replace(/^"(.*)"$/, '$1') : value;
}

function entries(section) {
  return Object.keys(section || {})
    .filter((key) => !key.endsWith('_comment'))
    .map((key) => [key, section[key]]);
}

function check(text, opts) {
  opts = opts || {};
  const sources = Array.from(new Set(SOURCES.concat(opts.sources || [])));
  const resources = Array.from(new Set(RESOURCES.concat(opts.resources || [])));
  const appDir = opts.appDir || null;
  const problems = [];

  let hash;
  try {
    hash = parser.parse(text);
  } catch (err) {
    return ['project.pbxproj does not parse: ' + (err && err.message)];
  }
  const objects = hash && hash.project && hash.project.objects;
  if (!objects) return ['project.pbxproj has no objects section'];
  // A value the writer could not fill in comes out as the word "undefined",
  // which Xcode would take literally.
  if (/=\s*undefined\s*;/.test(text)) problems.push('a value in the project is "undefined"');

  const refs = objects.PBXFileReference || {};
  const builds = objects.PBXBuildFile || {};
  const groups = objects.PBXGroup || {};
  const variants = objects.PBXVariantGroup || {};
  const products = objects.XCSwiftPackageProductDependency || {};
  const configs = objects.XCBuildConfiguration || {};
  const lists = objects.XCConfigurationList || {};

  // Every reference points at something.
  for (const [id, build] of entries(builds)) {
    if (build.fileRef && !refs[build.fileRef] && !variants[build.fileRef]) {
      problems.push('build file ' + id + ' points at a file reference that does not exist');
    }
    if (build.productRef && !products[build.productRef]) {
      problems.push('build file ' + id + ' points at a package product that does not exist');
    }
    if (!build.fileRef && !build.productRef) {
      problems.push('build file ' + id + ' names neither a file nor a package product');
    }
  }
  for (const [id, group] of entries(Object.assign({}, groups, variants))) {
    for (const child of group.children || []) {
      if (!refs[child.value] && !groups[child.value] && !variants[child.value]) {
        problems.push('group ' + (unquote(group.name) || unquote(group.path) || id) +
                      ' lists ' + child.value + ', which does not exist');
      }
    }
  }

  // The App target, and its Sources and Resources phases.
  const apps = entries(objects.PBXNativeTarget).filter(([, t]) => unquote(t.name) === 'App');
  if (apps.length !== 1) {
    problems.push('expected one target named App, found ' + apps.length);
    return problems;
  }
  const app = apps[0][1];
  function phaseOf(isa) {
    const section = objects[isa] || {};
    const found = (app.buildPhases || []).map((p) => p.value).filter((id) => section[id]);
    return found.length === 1 ? section[found[0]] : null;
  }
  const phases = { Sources: phaseOf('PBXSourcesBuildPhase'),
                   Resources: phaseOf('PBXResourcesBuildPhase') };
  for (const name of Object.keys(phases)) {
    if (!phases[name]) problems.push('the App target does not have exactly one ' + name + ' phase');
  }
  for (const name of Object.keys(phases)) {
    const seen = new Set();
    for (const file of (phases[name] && phases[name].files) || []) {
      if (!builds[file.value]) problems.push('the ' + name + ' phase lists a build file that does not exist');
      if (seen.has(file.value)) problems.push('the ' + name + ' phase lists a build file twice');
      seen.add(file.value);
    }
  }

  const appGroups = entries(groups).filter(([, g]) => unquote(g.path) === 'App');
  if (appGroups.length !== 1) {
    problems.push('expected one group for the App folder, found ' + appGroups.length);
    return problems;
  }
  const appGroup = appGroups[0][1];
  const inGroup = new Set((appGroup.children || []).map((c) => c.value));

  function registered(name, phaseName) {
    const found = entries(refs).filter(([, r]) => unquote(r.path) === name);
    if (found.length !== 1) {
      problems.push(name + ' has ' + found.length + ' file references, expected 1');
      return;
    }
    const refId = found[0][0];
    const ref = found[0][1];
    if (!inGroup.has(refId)) problems.push(name + ' is not in the App group');
    if (ref.explicitFileType !== undefined) problems.push(name + ' has an explicit file type');
    if (String(ref.includeInIndex) === '0') problems.push(name + ' is left out of Xcode\'s index');
    if (unquote(ref.sourceTree) !== '<group>') problems.push(name + ' is not relative to its group');
    const buildIds = entries(builds).filter(([, b]) => b.fileRef === refId).map(([id]) => id);
    if (buildIds.length !== 1) {
      problems.push(name + ' has ' + buildIds.length + ' build files, expected 1');
    }
    const phase = phases[phaseName];
    const listed = ((phase && phase.files) || []).filter((f) => buildIds.indexOf(f.value) >= 0);
    if (listed.length !== 1) {
      problems.push(name + ' is in the App target\'s ' + phaseName + ' phase ' + listed.length +
                    ' times, expected once');
    }
    if (appDir && !fs.existsSync(path.join(appDir, name))) {
      problems.push(name + ' is in the project but not on disk');
    }
  }
  sources.forEach((name) => registered(name, 'Sources'));
  resources.forEach((name) => registered(name, 'Resources'));

  // Everything the App group lists is on disk, apart from what cap sync makes.
  if (appDir) {
    for (const id of inGroup) {
      const files = refs[id] ? [refs[id]]
        : (variants[id] ? (variants[id].children || []).map((c) => refs[c.value]).filter(Boolean) : []);
      for (const ref of files) {
        const rel = unquote(ref.path);
        if (!rel || GENERATED.has(rel)) continue;
        if (!fs.existsSync(path.join(appDir, rel))) problems.push(rel + ' is listed in the App group but missing');
      }
    }
  }

  // iPhone only, in both of the App target's configurations.
  const list = lists[app.buildConfigurationList];
  const configIds = list ? (list.buildConfigurations || []).map((c) => c.value) : [];
  if (configIds.length !== 2) problems.push('the App target should have Debug and Release configurations');
  for (const id of configIds) {
    const config = configs[id];
    const settings = (config && config.buildSettings) || {};
    const family = unquote(String(settings.TARGETED_DEVICE_FAMILY));
    if (family !== '1') {
      problems.push('TARGETED_DEVICE_FAMILY is ' + family + ' in ' + unquote(config && config.name) +
                    ', expected 1 (iPhone only)');
    }
    if (unquote(String(settings.PRODUCT_BUNDLE_IDENTIFIER)) !== 'app.orenth.ios') {
      problems.push('the bundle identifier in ' + unquote(config && config.name) + ' is not app.orenth.ios');
    }
  }
  return problems;
}

module.exports = { check, PBXPROJ, APP_DIR };

if (require.main === module) {
  const problems = check(fs.readFileSync(PBXPROJ, 'utf-8'), { appDir: APP_DIR });
  if (problems.length) {
    console.error('project.pbxproj has problems:\n  ' + problems.join('\n  '));
    process.exit(1);
  }
  console.log('project.pbxproj parses, every reference resolves, the native files are in the App');
  console.log('target, and the app is iPhone-only.');
}
