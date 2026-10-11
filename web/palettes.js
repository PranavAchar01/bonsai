// Color palettes for the pixel-garden look. Shared by tree.html and palettes.html.
// bg: page, ink: text and lines, faint: secondary text, accent: pruned / cut marks, glow: CRT text glow.
export const PALETTES = {
  tokyo:    {name:'tokyo night',  bg:'#1a1b26', ink:'#7dcfff', faint:'#565f89', accent:'#f7768e', glow:true},
  garden:   {name:'night garden', bg:'#0f1712', ink:'#a9e2a0', faint:'#5f7f5a', accent:'#ff7a6a'},
  moss:     {name:'deep moss',    bg:'#0b1410', ink:'#c6f0b4', faint:'#6c8f63', accent:'#ffb38a'},
  jade:     {name:'jade terminal',bg:'#071311', ink:'#7fe0c2', faint:'#3f7f6c', accent:'#ff6b6b', glow:true},
  gameboy:  {name:'game boy',     bg:'#0f380f', ink:'#9bbc0f', faint:'#5c8a1f', accent:'#e0f8d0'},
  phosphor: {name:'phosphor',     bg:'#120d05', ink:'#ffb547', faint:'#8a6a3a', accent:'#ff5a3c', glow:true},
  neon:     {name:'neon bonsai',  bg:'#050608', ink:'#39ff88', faint:'#2a8a55', accent:'#ff3d6e', glow:true},
  lichen:   {name:'lichen',       bg:'#262a1f', ink:'#d4e09b', faint:'#8a9166', accent:'#f28c6b'},
  rust:     {name:'rust and moss',bg:'#1a120c', ink:'#9fcf7a', faint:'#7a6a52', accent:'#e86a33'},
  olive:    {name:'olive dusk',   bg:'#2b2a1e', ink:'#e6d9a8', faint:'#9a9070', accent:'#d9643a'},
  dusk:     {name:'sakura dusk',  bg:'#1e1424', ink:'#ffc2d1', faint:'#8a6a7f', accent:'#ff6f91'},
  indigo:   {name:'indigo washi', bg:'#1b2a4a', ink:'#e8dcc0', faint:'#8d93a8', accent:'#e0573f'},
  ice:      {name:'ice garden',   bg:'#0c1620', ink:'#bfe6ff', faint:'#5f7f98', accent:'#ff8a80'},
  matcha:   {name:'matcha',       bg:'#e3e8d0', ink:'#2f4a2a', faint:'#7d8a6a', accent:'#c2412d'},
  paper:    {name:'sumi paper',   bg:'#efe7d6', ink:'#1d1b19', faint:'#8b8272', accent:'#b5332a'},
};

// Dark when the background is closer to black than white.
export function isDark(hex) {
  const n = parseInt(hex.slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  return (.2126 * r + .7152 * g + .0722 * b) / 255 < .45;
}
