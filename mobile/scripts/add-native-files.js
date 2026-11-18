#!/usr/bin/env node
/*
 * Registers Orenth's own native files with the Xcode project, gives the app
 * its entitlements, and makes it iPhone-only.
 *
 * Files on disk are invisible to Xcode until project.pbxproj lists them.
 * This does what Xcode's "Add Files to App" would: a file reference in the
 * App group, and a build file in the App target's Sources phase (Swift) or
 * Resources phase (the privacy manifest). It uses the same library, and the
 * same calls, that the Capacitor CLI uses to register SceneDelegate.swift,
 * then reads its own output back to prove the project still parses and holds
 * every file.
 *
 * App.entitlements (Push Notifications, Sign in with Apple) is what Xcode's
 * "+ Capability" would write: a file reference in the App group, in no build
 * phase, named by CODE_SIGN_ENTITLEMENTS in both of the App target's
 * configurations. Kept in the project, a copy downloaded afresh is signed
 * with them, without anyone adding the capabilities again.
 *
 * Safe to run again: anything already registered is left alone.
 *
 *   cd mobile && node scripts/add-native-files.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const xcode = require('xcode');
const { check, ENTITLEMENTS, ENTITLEMENTS_SETTING } = require('./check-project');

const MOBILE = path.resolve(__dirname, '..');
const PBXPROJ = path.join(MOBILE, 'ios', 'App', 'App.xcodeproj', 'project.pbxproj');
const APP_DIR = path.join(MOBILE, 'ios', 'App', 'App');

// Paths relative to the App group, which is the App/App folder.
const SOURCES = ['MainViewController.swift', 'WebAuthPlugin.swift', 'AppPurchasePlugin.swift',
                 'NavigationGuardPlugin.swift'];
const RESOURCES = ['PrivacyInfo.xcprivacy'];

function groupKey(project, name) {
  const groups = project.hash.project.objects.PBXGroup;
  for (const key of Object.keys(groups)) {
    if (key.endsWith('_comment') && groups[key] === name) {
      return key.slice(0, -'_comment'.length);
    }
  }
  return null;
}

function main() {
  for (const name of SOURCES.concat(RESOURCES, [ENTITLEMENTS])) {
    if (!fs.existsSync(path.join(APP_DIR, name))) {
      throw new Error('Missing ' + path.join(APP_DIR, name));
    }
  }

  const project = xcode.project(PBXPROJ);
  project.parseSync();

  const target = project.getFirstTarget();
  if (!target || !target.firstTarget || String(target.firstTarget.name).replace(/"/g, '') !== 'App') {
    throw new Error('The first target is not "App"; this is not the project this script expects.');
  }
  const group = groupKey(project, 'App');
  if (!group) {
    throw new Error('No group named "App" in ' + PBXPROJ);
  }

  const original = fs.readFileSync(PBXPROJ, 'utf-8');
  const added = [];
  const refs = project.hash.project.objects.PBXFileReference;

  // The library writes "explicitFileType = undefined" and "includeInIndex = 0"
  // into a new file reference. Xcode reads an explicit type before the known
  // one, and leaves a file out of its index at 0, so each new reference is
  // rewritten in the exact form Xcode itself uses (as for SceneDelegate.swift).
  function plainReference(file, type) {
    if (!file || !file.fileRef || !refs[file.fileRef]) {
      throw new Error('No file reference was made for ' + (file && file.basename));
    }
    refs[file.fileRef] = {
      isa: 'PBXFileReference',
      lastKnownFileType: type,
      path: file.basename,
      sourceTree: '"<group>"'
    };
  }

  for (const name of SOURCES) {
    if (project.hasFile(name)) continue;
    const file = project.addSourceFile(name, { target: target.uuid }, group);
    if (!file) throw new Error('Could not add ' + name);
    plainReference(file, 'sourcecode.swift');
    added.push(name);
  }
  for (const name of RESOURCES) {
    if (project.hasFile(name)) continue;
    // Xcode records a privacy manifest as XML text.
    const file = project.addResourceFile(name, { target: target.uuid, lastKnownFileType: 'text.xml' }, group);
    if (!file) throw new Error('Could not add ' + name);
    plainReference(file, 'text.xml');
    added.push(name);
  }
  // Listed in the App group only: Xcode signs entitlements into the app, it
  // does not build or copy the file.
  if (!project.hasFile(ENTITLEMENTS)) {
    const file = project.addFile(ENTITLEMENTS, group, { lastKnownFileType: 'text.plist.entitlements' });
    if (!file) throw new Error('Could not add ' + ENTITLEMENTS);
    plainReference(file, 'text.plist.entitlements');
    added.push(ENTITLEMENTS);
  }

  // Both of the App target's configurations, nothing else: iPhone only, and
  // signed with the app's entitlements.
  project.updateBuildProperty('TARGETED_DEVICE_FAMILY', '1', null, 'App');
  project.updateBuildProperty('CODE_SIGN_ENTITLEMENTS', ENTITLEMENTS_SETTING, null, 'App');

  // Xcode keeps a configuration's settings in name order. So does this, so
  // a setting added here does not move the next time Xcode saves the project.
  const configs = project.pbxXCBuildConfigurationSection();
  const list = project.pbxXCConfigurationList()[target.firstTarget.buildConfigurationList];
  for (const entry of (list && list.buildConfigurations) || []) {
    const config = configs[entry.value];
    const sorted = {};
    for (const key of Object.keys(config.buildSettings).sort()) sorted[key] = config.buildSettings[key];
    config.buildSettings = sorted;
  }

  // The parser reads "0920" as the number 920. Same value, but keep the text.
  const attributes = project.getFirstProject().firstProject.attributes || {};
  for (const key of ['LastSwiftUpdateCheck', 'LastUpgradeCheck']) {
    const found = original.match(new RegExp('\\b' + key + ' = ([^;]+);'));
    if (found && attributes[key] !== undefined) attributes[key] = found[1];
  }

  const text = project.writeSync();
  const problems = check(text, { sources: SOURCES, resources: RESOURCES, appDir: APP_DIR });
  if (problems.length) {
    throw new Error('The written project would be wrong, so nothing was saved:\n  ' +
                    problems.join('\n  '));
  }
  fs.writeFileSync(PBXPROJ, text, 'utf-8');
  console.log(added.length ? 'Added: ' + added.join(', ') : 'Nothing new to add.');
  console.log('TARGETED_DEVICE_FAMILY = 1 (iPhone only).');
  console.log('CODE_SIGN_ENTITLEMENTS = ' + ENTITLEMENTS_SETTING + ' (Push Notifications, Sign in with Apple).');
  console.log('Checked: the project parses and lists every file.');
}

try {
  main();
} catch (err) {
  console.error(String(err && err.message || err));
  process.exit(1);
}
