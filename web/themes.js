/* Theme metadata is independent from reading data. Existing palette ids remain compatible. */
window.ExplorerThemes = {
  clay: {name:'暖纸书桌', description:'奶油纸色、赤陶与一盏阅读灯。', elements:'热茶 · 台灯 · 书页', caption:'WARM READING DESK', assetPrefix:'clay-', colors:['#9d5c43','#e6c4b3','#f7f2ea']},
  violet: {name:'暮光花笺', description:'柔粉与暮紫，把好奇夹进花笺。', elements:'压花 · 信笺 · 丝带', caption:'LET CURIOSITY BLOOM', assetPrefix:'violet-', colors:['#765985','#dfc8d4','#faf4f6']},
  parchment: {name:'旧书手札', description:'麦芽纸色与棕墨，慢慢积攒理解。', elements:'旧纸 · 邮戳 · 手札', caption:'ONE PAGE AT A TIME', assetPrefix:'parchment-', colors:['#805637','#d1b183','#f6eddb']},
  blue: {name:'星夜书房', description:'纸白与靛蓝，留一小片想象的星空。', elements:'月亮 · 星轨 · 星图', caption:'A LITTLE UNIVERSE OF IDEAS', assetPrefix:'', colors:['#4d5d95','#b5bfdc','#f7f6f2']},
  forest: {name:'林间晨读', description:'苔绿与晨光，在树影间慢慢读。', elements:'树影 · 叶片 · 晨光', caption:'READ BETWEEN THE LEAVES', assetPrefix:'forest-', colors:['#3f6b4f','#b6c6a4','#f3f5eb']},
  ocean: {name:'海盐晴空', description:'海蓝与云白，让思绪随风舒展。', elements:'海浪 · 帆船 · 云朵', caption:'SAIL INTO A NEW IDEA', assetPrefix:'ocean-', colors:['#2e6b86','#acd2dc','#f2f7f7']}
};
for(const [id,theme] of Object.entries(window.ExplorerThemes)){
  theme.avatar=`assets/generated/${id}-xiaotan-avatar.webp`;
  theme.portrait=`assets/generated/${id}-xiaotan-portrait.webp`;
  theme.scene=`assets/generated/${id}-reading-scene.webp`;
}
