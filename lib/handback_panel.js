// The "Hand back" panel on the Mac's screen while a handoff is open (lib/handoff.py run_panel; SECURITY.md,
// "Handing the computer to you"). JavaScript for Automation, run only as
//   /usr/bin/osascript -l JavaScript lib/handback_panel.js show WHO VM WHERE SLOT
// by handoff.py's helper, which reads what it prints: "handback" when the person clicks "Hand back to WHO", "cancel"
// for "Call it off", nothing otherwise. Only a click in this window answers: nothing in it reads a file, a variable or
// the network, and it never takes the keyboard (a non-activating panel that becomes key only if it must, which
// buttons don't), so keys meant for Screen Sharing never reach it. It floats above other windows, on every Space and
// beside a full-screen Screen Sharing. The helper takes it down when the handoff ends.
// VM and WHERE name the virtual computer ("mcinput2", "on this Mac"), in the title and the text, so panels for two VMs
// can't be taken for each other; SLOT (0 to SLOTS - 1) is its place on the screen (layout, below): places go down the
// right-hand side of the main screen's visible area, then in a column to the left of it, none covering another, and
// every one inside the visible area. `capacity` prints how many places fit on the main screen (the helper never uses
// more, and never one another panel holds), `layout X Y W H SLOT` where a place would be on a visible area of that size
// (the tests: small screens); neither shows anything.
// `selftest WHO VM WHERE SLOT` (the tests) builds the same panel off-screen, reports how it's set up and where it
// would be, and clicks its own button: its output goes to whoever ran it, never to the helper, which passes `show`.
ObjC.import('Cocoa');

// The panel's content (W x H), the margin from the screen's edges (M), its title bar (TB), the step from one place to
// the next below it (its title bar and a gap) and to the next column (GAP), and the places in all.
var W = 380, H = 104, M = 20, TB = 28, STEP = H + 44, GAP = 20, SLOTS = 12;

function grid(vw, vh) {
  var rows = Math.max(1, Math.floor((vh - 2 * M - TB - H) / STEP) + 1);
  var cols = Math.max(1, Math.floor((vw - 2 * M - W) / (W + GAP)) + 1);
  return {rows: rows, cols: cols, capacity: Math.max(1, Math.min(SLOTS, rows * cols))};
}

// Where place SLOT is on a visible area (VX, VY: its bottom left; VW x VH): the content's bottom left, and whether
// it fits whole without covering another place. One that doesn't (a screen smaller than the helper was told) is
// still kept inside the area.
function layout(vx, vy, vw, vh, slot) {
  var g = grid(vw, vh), row = slot % g.rows, col = Math.floor(slot / g.rows);
  var x = vx + vw - M - W - col * (W + GAP), y = vy + vh - M - TB - H - row * STEP;
  var fits = slot < g.capacity && x >= vx && y >= vy;
  x = Math.max(vx, Math.min(x, vx + vw - W));
  y = Math.max(vy, Math.min(y, vy + vh - TB - H));
  return {x: Math.round(x), y: Math.round(y), fits: fits, capacity: g.capacity};
}

// Its command lines (metacomputer's command-line rules, as far as osascript lets a script keep them): exactly these
// forms, or nothing is done and osascript exits non-zero (the helper takes that as no answer). No word starts with '-'
// (no options, no negative numbers), a SLOT is a whole number from 0 to SLOTS - 1 written plainly, and the layout's
// sizes are plain numbers.
var FORMS = {show: 5, selftest: 5, capacity: 1, layout: 6};

function usage() {
  throw new Error('usage: handback_panel.js show WHO VM WHERE SLOT | selftest [WHO [VM [WHERE [SLOT]]]] | capacity | '
                  + 'layout X Y W H SLOT (run by lib/handoff.py; nothing was done)');
}

function slotOf(w) {
  var t = String(w);
  if (!/^(0|[1-9][0-9]?)$/.test(t) || parseInt(t, 10) >= SLOTS) usage();
  return parseInt(t, 10);
}

function run(argv) {
  var mode = argv[0];
  if (!Object.prototype.hasOwnProperty.call(FORMS, mode) || argv.length > FORMS[mode]
      || (mode !== 'selftest' && argv.length !== FORMS[mode])
      || argv.some(function (w) { return String(w).charAt(0) === '-'; })) {
    usage();
  }
  if (mode === 'layout') {
    var a = [1, 2, 3, 4].map(function (i) {
      return /^[0-9]{1,6}(\.[0-9]{1,6})?$/.test(String(argv[i])) ? Number(argv[i]) : NaN;
    });
    if (a.some(isNaN) || a[2] <= 0 || a[3] <= 0) usage();
    a.push(slotOf(argv[5]));
    var l = layout(a[0], a[1], a[2], a[3], a[4]);
    return [l.x, l.y, W, H].join(',') + ' fits=' + l.fits + ' capacity=' + l.capacity + ' titlebar=' + TB;
  }
  if (mode === 'capacity') {
    var v = $.NSScreen.mainScreen.visibleFrame;
    return String(grid(v.size.width, v.size.height).capacity);
  }
  var who = String(argv[1] || 'Claude'), vm = String(argv[2] || ''), where = String(argv[3] || '');
  var slot = argv.length > 4 ? slotOf(argv[4]) : 0;
  if (!/^[A-Za-z ]{1,20}$/.test(who)) who = 'Claude';
  if (!/^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$/.test(vm)) vm = 'the virtual computer';   // scripts/lib.sh's VM name rule
  if (!/^[A-Za-z0-9 .'()-]{1,40}$/.test(where)) where = '';
  var app = $.NSApplication.sharedApplication;
  app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);   // no Dock icon, never the active app
  var answer = '';
  ObjC.registerSubclass({
    name: 'MCHandBackTarget',
    methods: {
      'handBack:': {types: ['void', ['id']], implementation: function (sender) { answer = 'handback'; $.NSApp.stop(null); }},
      'callOff:': {types: ['void', ['id']], implementation: function (sender) { answer = 'cancel'; $.NSApp.stop(null); }}
    }
  });
  var target = $.MCHandBackTarget.alloc.init;
  var area = $.NSScreen.mainScreen.visibleFrame;
  var spot = layout(area.origin.x, area.origin.y, area.size.width, area.size.height, slot);
  var x = spot.x, y = spot.y;
  var placed = [x, y, W, H].join(',');
  if (mode === 'selftest') { x = -20000; y = -20000; }
  var style = $.NSWindowStyleMaskTitled | $.NSWindowStyleMaskUtilityWindow | $.NSWindowStyleMaskNonactivatingPanel;
  var panel = $.NSPanel.alloc.initWithContentRectStyleMaskBackingDefer($.NSMakeRect(x, y, W, H), style,
                                                                      $.NSBackingStoreBuffered, false);
  panel.title = 'metacomputer: ' + vm;
  panel.level = $.NSFloatingWindowLevel;
  panel.floatingPanel = true;
  panel.hidesOnDeactivate = false;
  panel.becomesKeyOnlyIfNeeded = true;
  panel.collectionBehavior = $.NSWindowCollectionBehaviorCanJoinAllSpaces |
                             $.NSWindowCollectionBehaviorFullScreenAuxiliary;
  var label = $.NSTextField.labelWithString(who + ' is waiting while you use ' + vm + (where ? ' ' + where : '') + '.');
  label.frame = $.NSMakeRect(16, 60, W - 32, 20);
  var back = $.NSButton.buttonWithTitleTargetAction('Hand back to ' + who, target, 'handBack:');
  back.frame = $.NSMakeRect(12, 16, 196, 32);
  var off = $.NSButton.buttonWithTitleTargetAction('Call it off', target, 'callOff:');
  off.frame = $.NSMakeRect(216, 16, 112, 32);
  panel.contentView.addSubview(label);
  panel.contentView.addSubview(back);
  panel.contentView.addSubview(off);
  panel.orderFrontRegardless;                                         // shown without activating this app
  if (mode === 'selftest') {
    var setup = ['visible=' + panel.isVisible, 'level=' + panel.level, 'floating=' + panel.isFloatingPanel,
                 'nonactivating=' + ((panel.styleMask & $.NSWindowStyleMaskNonactivatingPanel) !== 0),
                 'key=' + panel.isKeyWindow, 'active=' + app.isActive, 'title=' + back.title.js,
                 'window=' + panel.title.js.replace(/ /g, '_'), 'text=' + label.stringValue.js.replace(/ /g, '_'),
                 'frame=' + placed, 'step=' + STEP, 'fits=' + spot.fits].join(' ');
    back.performClick(null);
    return 'selftest ' + setup + ' answer=' + answer;
  }
  app.run;
  return answer;
}
