export interface HeroIdentity {
  readonly englishName: string;
  readonly officialName: string;
  readonly aliases: readonly string[];
}

export const HERO_IDENTITIES: readonly HeroIdentity[] = [
  { englishName: 'Adagio', officialName: '奥达基', aliases: ['鸟人'] },
  { englishName: 'Alpha', officialName: '阿尔法', aliases: ['阿尔法'] },
  { englishName: 'Amael', officialName: '阿玛尔', aliases: ['牛头'] },
  { englishName: 'Anka', officialName: '安卡', aliases: ['安卡'] },
  { englishName: 'Ardan', officialName: '亚丹', aliases: ['二蛋'] },
  { englishName: 'Baptiste', officialName: '巴蒂斯特', aliases: ['巴蒂'] },
  { englishName: 'Baron', officialName: '巴隆', aliases: ['巴隆'] },
  {
    englishName: 'Blackfeather',
    officialName: '黑羽',
    aliases: ['黑羽'],
  },
  { englishName: 'Caine', officialName: '凯恩', aliases: ['凯恩'] },
  { englishName: 'Catherine', officialName: '凯瑟琳', aliases: ['女警'] },
  { englishName: 'Celeste', officialName: '星乐斯', aliases: ['星妈'] },
  {
    englishName: 'Churnwalker',
    officialName: '沃克尔',
    aliases: ['沃克尔'],
  },
  { englishName: 'Flicker', officialName: '弗利克', aliases: ['小精灵'] },
  { englishName: 'Fortress', officialName: '福彻斯', aliases: ['魔狼'] },
  { englishName: 'Glaive', officialName: '格雷', aliases: ['豹子'] },
  { englishName: 'Grace', officialName: '格瑞丝', aliases: ['锤妈'] },
  { englishName: 'Grumpjaw', officialName: '格兰卓', aliases: ['大嘴'] },
  { englishName: 'Gwen', officialName: '格温', aliases: ['女枪'] },
  { englishName: 'Idris', officialName: '伊德瑞', aliases: ['伊德瑞'] },
  { englishName: 'Inara', officialName: '伊娜', aliases: ['伊娜'] },
  { englishName: 'Ishtar', officialName: '伊丝塔', aliases: ['伊斯塔'] },
  { englishName: 'Joule', officialName: '朱尔', aliases: ['猪儿'] },
  { englishName: 'Karas', officialName: '鸦', aliases: ['鸦'] },
  { englishName: 'Kensei', officialName: '肯赛', aliases: ['剑圣'] },
  { englishName: 'Kestrel', officialName: '凯思卓', aliases: ['鹰眼'] },
  { englishName: 'Kinetic', officialName: '基妮', aliases: ['基尼'] },
  { englishName: 'Koshka', officialName: '柯思卡', aliases: ['猫女'] },
  { englishName: 'Krul', officialName: '骷髅', aliases: ['鬼剑'] },
  { englishName: 'Lance', officialName: '兰斯', aliases: ['兰斯'] },
  { englishName: 'Leo', officialName: '里昂', aliases: ['里昂'] },
  { englishName: 'Lorelai', officialName: '洛姬', aliases: ['蛇女'] },
  { englishName: 'Lyra', officialName: '莱拉', aliases: ['莱拉'] },
  { englishName: 'Magnus', officialName: '玛格纳斯', aliases: ['马哥'] },
  { englishName: 'Malene', officialName: '梅兰妮', aliases: ['小公主'] },
  { englishName: 'Miho', officialName: '美惠', aliases: ['美慧'] },
  { englishName: 'Ozo', officialName: '奥佐', aliases: ['猴子'] },
  { englishName: 'Petal', officialName: '佩兔', aliases: ['花花'] },
  { englishName: 'Phinn', officialName: '费恩', aliases: ['鱼人'] },
  { englishName: 'Reim', officialName: '莱姆', aliases: ['老头'] },
  { englishName: 'Reza', officialName: '雷萨', aliases: ['火法'] },
  { englishName: 'Ringo', officialName: '林戈', aliases: ['酒枪'] },
  { englishName: 'Rona', officialName: '罗娜', aliases: ['罗娜'] },
  { englishName: 'Samuel', officialName: '萨缪尔', aliases: ['黑法'] },
  { englishName: 'Sanfeng', officialName: '三风', aliases: ['三丰'] },
  { englishName: 'SAW', officialName: '索尔', aliases: ['索尔', '机枪'] },
  { englishName: 'Shin', officialName: '哪吒', aliases: ['哪吒'] },
  { englishName: 'Silvernail', officialName: '西弗尔', aliases: ['银锭'] },
  { englishName: 'Skaarf', officialName: '史卡夫', aliases: ['火龙'] },
  { englishName: 'Skye', officialName: '丝凯伊', aliases: ['斯凯伊'] },
  { englishName: 'Taka', officialName: '塔卡', aliases: ['塔卡'] },
  { englishName: 'Tony', officialName: '托尼', aliases: ['托尼'] },
  { englishName: 'Varya', officialName: '瓦妮亚', aliases: ['雷妈'] },
  { englishName: 'Viola', officialName: '维奥拉', aliases: ['维奥拉'] },
  { englishName: 'Vox', officialName: '舞司', aliases: ['舞司'] },
  { englishName: 'Warhawk', officialName: '尼尔', aliases: ['小炮'] },
  { englishName: 'Yates', officialName: '耶茨', aliases: ['椰子'] },
  { englishName: 'Ylva', officialName: '伊娃', aliases: ['伊娃'] },
] as const;

const HERO_IDENTITIES_BY_ENGLISH_NAME = new Map(
  HERO_IDENTITIES.map((hero) => [hero.englishName.toLocaleLowerCase(), hero]),
);

export function heroIdentity(englishName: string): HeroIdentity | undefined {
  return HERO_IDENTITIES_BY_ENGLISH_NAME.get(englishName.toLocaleLowerCase());
}

export function heroDisplayName(englishName: string): string {
  const identity = heroIdentity(englishName);
  return identity?.aliases[0] ?? identity?.officialName ?? englishName;
}

export function heroSearchSegments(englishName: string): readonly string[] {
  const identity = heroIdentity(englishName);
  if (identity === undefined) {
    return [englishName];
  }
  return [identity.officialName, identity.englishName, ...identity.aliases];
}
