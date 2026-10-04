// SPDX-License-Identifier: GPL-2.0-only
/*
 * X-Powers AC108 quad ADC, as wired on the Seeed ReSpeaker Core v2.
 *
 * Port of Seeed's ac108.c v2.0 (panjunwen / Baozhu Zuo) for the RK3229 4.4
 * kernel to the current ASoC component API, without its global state and
 * its simple-card hack (the PLL is now configured from hw_params).
 *
 * The board has two chips on one I2S link. simple-audio-card supports a
 * single codec per link, so, as in the original driver, one chip (the one
 * with a DAI) drives the others: it lists them in "x-powers,secondary-chips"
 * and applies every operation to the whole group. Secondary chips only
 * provide their registers.
 *
 * Each chip is an I2S bit/frame clock consumer, clocks itself from BCLK
 * (64 fs) through its PLL and sends two stereo lanes with 32-bit slots:
 * SDO1 carries ADC1/ADC2 and SDO2 ADC3/ADC4.
 *
 * Device tree:
 *   compatible = "x-powers,ac108";
 *   #sound-dai-cells = <0>;              primary chip only
 *   x-powers,secondary-chips = <&...>;   primary chip only
 *   x-powers,channel-base = <4>;         number of the chip's first channel,
 *                                        minus one (mixer control names)
 *   bclk-en-gpios = <...>;               optional, raised while capturing
 */

#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/regmap.h>
#include <sound/pcm_params.h>
#include <sound/soc.h>
#include <sound/tlv.h>

#include "ac108.h"

/* BCLK is always 64 fs on this design: two 32-bit slots per frame */
#define AC108_BCLK_FS		64
#define AC108_SLOT_WIDTH	32
#define AC108_LRCK_PERIOD	AC108_SLOT_WIDTH

#define AC108_MAX_CHIPS		4
#define AC108_CTLS_PER_CHIP	8

#define AC108_RATES	(SNDRV_PCM_RATE_8000 | SNDRV_PCM_RATE_11025 | \
			 SNDRV_PCM_RATE_12000 | SNDRV_PCM_RATE_16000 | \
			 SNDRV_PCM_RATE_22050 | SNDRV_PCM_RATE_24000 | \
			 SNDRV_PCM_RATE_32000 | SNDRV_PCM_RATE_44100 | \
			 SNDRV_PCM_RATE_48000 | SNDRV_PCM_RATE_96000)
#define AC108_FORMATS	(SNDRV_PCM_FMTBIT_S16_LE | SNDRV_PCM_FMTBIT_S20_3LE | \
			 SNDRV_PCM_FMTBIT_S24_LE | SNDRV_PCM_FMTBIT_S32_LE)

/* One per chip (I2C device) */
struct ac108_chip {
	struct regmap *regmap;
	struct gpio_desc *bclk_en;
	u32 channel_base;
};

/* A mixer control: one register field of one chip */
struct ac108_ctl {
	struct regmap *regmap;
	unsigned int reg;
	unsigned int shift;
	unsigned int max;
};

/* The primary chip: itself first, then its secondaries */
struct ac108_priv {
	struct ac108_chip *chips[AC108_MAX_CHIPS];
	int num_chips;
};

#define for_each_chip(priv, i, c) \
	for (i = 0; i < (priv)->num_chips && ((c) = (priv)->chips[i]); i++)

struct ac108_reg_val {
	unsigned int val;
	unsigned int reg;
};

static const struct ac108_reg_val ac108_sample_rate[] = {
	{ 8000,  0 }, { 11025, 1 }, { 12000, 2 }, { 16000, 3 }, { 22050, 4 },
	{ 24000, 5 }, { 32000, 6 }, { 44100, 7 }, { 48000, 8 }, { 96000, 9 },
};

static const struct ac108_reg_val ac108_sample_resolution[] = {
	{ 8, 1 }, { 12, 2 }, { 16, 3 }, { 20, 4 }, { 24, 5 }, { 28, 6 }, { 32, 7 },
};

/* FOUT = FIN * N / ((M1 + 1) * (M2 + 1) * (K1 + 1) * (K2 + 1)) */
struct ac108_pll_div {
	u32 freq_in;
	u8 m1, m2;
	u16 n;
	u8 k1, k2;
};

/* PLL settings for BCLK = 64 fs, from the original driver's table */
static const struct ac108_pll_div ac108_pll_div[] = {
	{ 512000,  0, 0, 960, 9, 1 },	/*  8 kHz    -> 24.576 MHz */
	{ 705600,  0, 0, 640, 9, 1 },	/* 11.025kHz -> 22.5792 MHz */
	{ 768000,  0, 0, 640, 9, 1 },	/* 12 kHz    -> 24.576 MHz */
	{ 1024000, 0, 0, 480, 9, 1 },	/* 16 kHz */
	{ 1411200, 0, 0, 320, 9, 1 },	/* 22.05 kHz */
	{ 1536000, 0, 0, 320, 9, 1 },	/* 24 kHz */
	{ 2048000, 0, 0, 240, 9, 1 },	/* 32 kHz */
	{ 2822400, 0, 0, 160, 9, 1 },	/* 44.1 kHz */
	{ 3072000, 0, 0, 160, 9, 1 },	/* 48 kHz */
	{ 6144000, 4, 0, 400, 9, 1 },	/* 96 kHz */
};

/* Digital channel volume: -119.25 dB (0) to +72 dB (255), 0.75 dB steps, 0 dB at 0xa0 */
static const DECLARE_TLV_DB_SCALE(ac108_ch_vol_tlv, -11925, 75, 0);
static const DECLARE_TLV_DB_SCALE(ac108_pga_gain_tlv, 0, 100, 0);

static const struct {
	const char *fmt;	/* control name, %u = channel number */
	unsigned int reg, shift, max;
	const unsigned int *tlv;
} ac108_ctl_tmpl[AC108_CTLS_PER_CHIP] = {
	{ "CH%u Capture Volume", ADC1_DVOL_CTRL, 0, 0xff, ac108_ch_vol_tlv },
	{ "CH%u Capture Volume", ADC2_DVOL_CTRL, 0, 0xff, ac108_ch_vol_tlv },
	{ "CH%u Capture Volume", ADC3_DVOL_CTRL, 0, 0xff, ac108_ch_vol_tlv },
	{ "CH%u Capture Volume", ADC4_DVOL_CTRL, 0, 0xff, ac108_ch_vol_tlv },
	{ "ADC%u PGA Capture Volume", ANA_PGA1_CTRL, ADC1_ANALOG_PGA, 0x1f, ac108_pga_gain_tlv },
	{ "ADC%u PGA Capture Volume", ANA_PGA2_CTRL, ADC2_ANALOG_PGA, 0x1f, ac108_pga_gain_tlv },
	{ "ADC%u PGA Capture Volume", ANA_PGA3_CTRL, ADC3_ANALOG_PGA, 0x1f, ac108_pga_gain_tlv },
	{ "ADC%u PGA Capture Volume", ANA_PGA4_CTRL, ADC4_ANALOG_PGA, 0x1f, ac108_pga_gain_tlv },
};

static int ac108_ctl_info(struct snd_kcontrol *kcontrol,
			  struct snd_ctl_elem_info *uinfo)
{
	struct ac108_ctl *ctl = (struct ac108_ctl *)kcontrol->private_value;

	uinfo->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
	uinfo->count = 1;
	uinfo->value.integer.min = 0;
	uinfo->value.integer.max = ctl->max;
	return 0;
}

static int ac108_ctl_get(struct snd_kcontrol *kcontrol,
			 struct snd_ctl_elem_value *ucontrol)
{
	struct ac108_ctl *ctl = (struct ac108_ctl *)kcontrol->private_value;
	unsigned int val;
	int ret;

	ret = regmap_read(ctl->regmap, ctl->reg, &val);
	if (ret)
		return ret;
	ucontrol->value.integer.value[0] = (val >> ctl->shift) & ctl->max;
	return 0;
}

static int ac108_ctl_put(struct snd_kcontrol *kcontrol,
			 struct snd_ctl_elem_value *ucontrol)
{
	struct ac108_ctl *ctl = (struct ac108_ctl *)kcontrol->private_value;
	long val = ucontrol->value.integer.value[0];
	bool changed;
	int ret;

	if (val < 0 || val > ctl->max)
		return -EINVAL;
	ret = regmap_update_bits_check(ctl->regmap, ctl->reg,
				       ctl->max << ctl->shift, val << ctl->shift,
				       &changed);
	return ret ? ret : changed;
}

/* Power up the analog part, the clocks and the I2S transmitter layout. */
static void ac108_hw_init(struct regmap *map)
{
	/* Analog LDO, VREF (fast start) and VREFP (fast start) */
	regmap_write(map, PWR_CTRL6, 0x01);
	regmap_write(map, PWR_CTRL7, 0x9b);
	regmap_write(map, PWR_CTRL9, 0x81);
	/* Bias current of the DSM integrator opamps */
	regmap_write(map, ANA_ADC3_CTRL7, 0x03);

	regmap_update_bits(map, SYSCLK_CTRL, BIT(SYSCLK_EN), BIT(SYSCLK_EN));
	/* I2S, ADC digital, MIC offset calibration and ADC analog: clocks on, out of reset */
	regmap_write(map, MOD_CLK_EN, 0x93);
	regmap_write(map, MOD_RST_CTRL, 0x93);

	/* Both serial outputs, SDO drive and sample on different BCLK edges */
	regmap_update_bits(map, I2S_CTRL, BIT(SDO1_EN) | BIT(SDO2_EN),
			   BIT(SDO1_EN) | BIT(SDO2_EN));
	regmap_update_bits(map, I2S_BCLK_CTRL, BIT(EDGE_TRANSFER), 0);
	regmap_update_bits(map, I2S_LRCK_CTRL1, 0x3 << LRCK_PERIODH,
			   ((AC108_LRCK_PERIOD - 1) >> 8) << LRCK_PERIODH);
	regmap_write(map, I2S_LRCK_CTRL2, (AC108_LRCK_PERIOD - 1) & 0xff);
	/* No encoding mode, no hi-z between slots, transfer state enabled */
	regmap_update_bits(map, I2S_FMT_CTRL1,
			   BIT(ENCD_SEL) | BIT(TX_SLOT_HIZ) | BIT(TX_STATE),
			   BIT(TX_STATE));
	regmap_update_bits(map, I2S_FMT_CTRL2, 0x7 << SLOT_WIDTH_SEL,
			   7 << SLOT_WIDTH_SEL);	/* 32-bit slots */
	/* MSB first, pad with zeros, short frame, linear PCM */
	regmap_write(map, I2S_FMT_CTRL3, 0x60);

	/* SDO1: CH1/CH2 <- ADC1/ADC2, SDO2: CH1/CH2 <- ADC3/ADC4 */
	regmap_write(map, I2S_TX1_CHMP_CTRL1, 0x04);
	regmap_write(map, I2S_TX1_CHMP_CTRL2, 0x00);
	regmap_write(map, I2S_TX1_CHMP_CTRL3, 0x00);
	regmap_write(map, I2S_TX1_CHMP_CTRL4, 0x00);
	regmap_write(map, I2S_TX2_CHMP_CTRL1, 0x0e);
	regmap_write(map, I2S_TX2_CHMP_CTRL2, 0x00);
	regmap_write(map, I2S_TX2_CHMP_CTRL3, 0x00);
	regmap_write(map, I2S_TX2_CHMP_CTRL4, 0x00);

	/* ADC digital part, then un-gate the ADC clocks */
	regmap_write(map, ADC_DIG_EN, 0x1f);
	regmap_write(map, ANA_ADC4_CTRL7, 0x0f);

	/* AAF, ADC, PGA and MICBIAS on, unmuted, for all 4 inputs */
	regmap_write(map, ANA_ADC1_CTRL1, 0x07);
	regmap_write(map, ANA_ADC2_CTRL1, 0x07);
	regmap_write(map, ANA_ADC3_CTRL1, 0x07);
	regmap_write(map, ANA_ADC4_CTRL1, 0x07);

	/* Let VREF/VREFP settle, then leave fast-start mode */
	msleep(50);
	regmap_update_bits(map, PWR_CTRL7, BIT(VREF_FASTSTART_ENABLE), 0);
	regmap_update_bits(map, PWR_CTRL9, BIT(VREFP_FASTSTART_ENABLE), 0);
}

static int ac108_set_pll_from_bclk(struct regmap *map, unsigned int freq_in)
{
	const struct ac108_pll_div *div = NULL;
	int i;

	for (i = 0; i < ARRAY_SIZE(ac108_pll_div); i++) {
		if (ac108_pll_div[i].freq_in == freq_in) {
			div = &ac108_pll_div[i];
			break;
		}
	}
	if (!div)
		return -EINVAL;

	regmap_update_bits(map, SYSCLK_CTRL, 0x3 << PLLCLK_SRC,
			   PLLCLK_SRC_BCLK << PLLCLK_SRC);
	regmap_update_bits(map, PLL_CTRL2, 0x1f << PLL_PREDIV1 | BIT(PLL_PREDIV2),
			   div->m1 << PLL_PREDIV1 | div->m2 << PLL_PREDIV2);
	regmap_update_bits(map, PLL_CTRL3, 0x3 << PLL_LOOPDIV_MSB,
			   (div->n >> 8) << PLL_LOOPDIV_MSB);
	regmap_update_bits(map, PLL_CTRL4, 0xff << PLL_LOOPDIV_LSB,
			   (div->n & 0xff) << PLL_LOOPDIV_LSB);
	regmap_update_bits(map, PLL_CTRL5, 0x1f << PLL_POSTDIV1 | BIT(PLL_POSTDIV2),
			   div->k1 << PLL_POSTDIV1 | div->k2 << PLL_POSTDIV2);
	regmap_update_bits(map, PLL_CTRL1, 0x7 << PLL_IBIAS, 0);

	regmap_update_bits(map, PLL_LOCK_CTRL, BIT(PLL_LOCK_EN), BIT(PLL_LOCK_EN));
	regmap_update_bits(map, PLL_CTRL1, BIT(PLL_EN) | BIT(PLL_COM_EN),
			   BIT(PLL_EN) | BIT(PLL_COM_EN));
	/* SYSCLK from the PLL */
	regmap_update_bits(map, SYSCLK_CTRL,
			   BIT(PLLCLK_EN) | BIT(SYSCLK_SRC) | BIT(SYSCLK_EN),
			   BIT(PLLCLK_EN) | SYSCLK_SRC_PLL << SYSCLK_SRC | BIT(SYSCLK_EN));
	return 0;
}

static void ac108_set_bclk_en(struct ac108_priv *priv, int on, bool can_sleep)
{
	struct ac108_chip *c;
	int i;

	for_each_chip(priv, i, c) {
		if (can_sleep)
			gpiod_set_value_cansleep(c->bclk_en, on);
		else
			gpiod_set_value(c->bclk_en, on);
	}
}

static int ac108_set_fmt(struct snd_soc_dai *dai, unsigned int fmt)
{
	struct ac108_priv *priv = snd_soc_component_get_drvdata(dai->component);
	unsigned int mode, offset, bclk_pol, lrck_pol, ioen;
	struct ac108_chip *c;
	int i;

	switch (fmt & SND_SOC_DAIFMT_CLOCK_PROVIDER_MASK) {
	case SND_SOC_DAIFMT_CBC_CFC:
		ioen = 0;			/* BCLK and LRCK are inputs */
		break;
	case SND_SOC_DAIFMT_CBP_CFP:
		ioen = 0x3 << LRCK_IOEN;	/* driven by the primary chip */
		break;
	default:
		return -EINVAL;
	}

	switch (fmt & SND_SOC_DAIFMT_FORMAT_MASK) {
	case SND_SOC_DAIFMT_I2S:
		mode = LEFT_JUSTIFIED_FORMAT;
		offset = 1;
		break;
	case SND_SOC_DAIFMT_RIGHT_J:
		mode = RIGHT_JUSTIFIED_FORMAT;
		offset = 0;
		break;
	case SND_SOC_DAIFMT_LEFT_J:
		mode = LEFT_JUSTIFIED_FORMAT;
		offset = 0;
		break;
	case SND_SOC_DAIFMT_DSP_A:
		mode = PCM_FORMAT;
		offset = 1;
		break;
	case SND_SOC_DAIFMT_DSP_B:
		mode = PCM_FORMAT;
		offset = 0;
		break;
	default:
		return -EINVAL;
	}

	switch (fmt & SND_SOC_DAIFMT_INV_MASK) {
	case SND_SOC_DAIFMT_NB_NF:
		bclk_pol = BCLK_NORMAL_DRIVE_N_SAMPLE_P;
		lrck_pol = LRCK_LEFT_LOW_RIGHT_HIGH;
		break;
	case SND_SOC_DAIFMT_NB_IF:
		bclk_pol = BCLK_NORMAL_DRIVE_N_SAMPLE_P;
		lrck_pol = LRCK_LEFT_HIGH_RIGHT_LOW;
		break;
	case SND_SOC_DAIFMT_IB_NF:
		bclk_pol = BCLK_INVERT_DRIVE_P_SAMPLE_N;
		lrck_pol = LRCK_LEFT_LOW_RIGHT_HIGH;
		break;
	case SND_SOC_DAIFMT_IB_IF:
		bclk_pol = BCLK_INVERT_DRIVE_P_SAMPLE_N;
		lrck_pol = LRCK_LEFT_HIGH_RIGHT_LOW;
		break;
	default:
		return -EINVAL;
	}

	for_each_chip(priv, i, c) {
		/* Only the primary chip may drive the clocks */
		regmap_update_bits(c->regmap, I2S_CTRL, 0x3 << LRCK_IOEN,
				   i == 0 ? ioen : 0);
		regmap_update_bits(c->regmap, I2S_FMT_CTRL1,
				   0x3 << MODE_SEL | BIT(TX2_OFFSET) | BIT(TX1_OFFSET),
				   mode << MODE_SEL | offset << TX2_OFFSET | offset << TX1_OFFSET);
		regmap_update_bits(c->regmap, I2S_BCLK_CTRL, BIT(BCLK_POLARITY),
				   bclk_pol << BCLK_POLARITY);
		regmap_update_bits(c->regmap, I2S_LRCK_CTRL1, BIT(LRCK_POLARITY),
				   lrck_pol << LRCK_POLARITY);
	}
	return 0;
}

static int ac108_hw_params(struct snd_pcm_substream *substream,
			   struct snd_pcm_hw_params *params,
			   struct snd_soc_dai *dai)
{
	struct ac108_priv *priv = snd_soc_component_get_drvdata(dai->component);
	unsigned int rate = params_rate(params);
	int width = params_width(params);
	int i, r, w, ret;
	struct ac108_chip *c;

	for (r = 0; r < ARRAY_SIZE(ac108_sample_rate); r++)
		if (ac108_sample_rate[r].val == rate)
			break;
	for (w = 0; w < ARRAY_SIZE(ac108_sample_resolution); w++)
		if (ac108_sample_resolution[w].val == width)
			break;
	if (r == ARRAY_SIZE(ac108_sample_rate) ||
	    w == ARRAY_SIZE(ac108_sample_resolution))
		return -EINVAL;

	for_each_chip(priv, i, c) {
		struct regmap *map = c->regmap;

		ac108_hw_init(map);

		ret = ac108_set_pll_from_bclk(map, rate * AC108_BCLK_FS);
		if (ret) {
			dev_err(dai->dev, "no PLL setting for %u Hz\n", rate);
			return ret;
		}

		regmap_update_bits(map, ADC_SPRC, 0xf << ADC_FS_I2S1,
				   ac108_sample_rate[r].reg << ADC_FS_I2S1);

		/* Two channels (CH1, CH2) on each serial output */
		regmap_write(map, I2S_TX1_CTRL1, 0x01);
		regmap_write(map, I2S_TX1_CTRL2, 0x03);
		regmap_write(map, I2S_TX1_CTRL3, 0x00);
		regmap_write(map, I2S_TX2_CTRL1, 0x01);
		regmap_write(map, I2S_TX2_CTRL2, 0x03);
		regmap_write(map, I2S_TX2_CTRL3, 0x00);

		regmap_update_bits(map, I2S_FMT_CTRL2, 0x7 << SAMPLE_RESOLUTION,
				   ac108_sample_resolution[w].reg << SAMPLE_RESOLUTION);

		/* Transmitter and I2S block on */
		regmap_update_bits(map, I2S_CTRL, BIT(TXEN) | BIT(GEN),
				   BIT(TXEN) | BIT(GEN));
	}

	/* All chips are set up: let BCLK through, they start together */
	ac108_set_bclk_en(priv, 1, true);
	return 0;
}

static int ac108_hw_free(struct snd_pcm_substream *substream,
			 struct snd_soc_dai *dai)
{
	struct ac108_priv *priv = snd_soc_component_get_drvdata(dai->component);
	struct ac108_chip *c;
	int i;

	ac108_set_bclk_en(priv, 0, true);

	/*
	 * Stop the PLLs so that every chip restarts its PLL in sync with the
	 * others on the next stream (the original driver's fix for drifting
	 * chips).
	 */
	for_each_chip(priv, i, c) {
		regmap_update_bits(c->regmap, PLL_CTRL1, BIT(PLL_EN) | BIT(PLL_COM_EN), 0);
		regmap_update_bits(c->regmap, MOD_CLK_EN, BIT(ADC_DIGITAL), 0);
		regmap_update_bits(c->regmap, MOD_RST_CTRL, BIT(ADC_DIGITAL), 0);
		regmap_update_bits(c->regmap, I2S_CTRL, BIT(GEN), 0);
	}
	return 0;
}

static int ac108_trigger(struct snd_pcm_substream *substream, int cmd,
			 struct snd_soc_dai *dai)
{
	struct ac108_priv *priv = snd_soc_component_get_drvdata(dai->component);

	/* Atomic context: only the (non-sleeping) BCLK enable GPIO here */
	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
	case SNDRV_PCM_TRIGGER_RESUME:
	case SNDRV_PCM_TRIGGER_PAUSE_RELEASE:
		ac108_set_bclk_en(priv, 1, false);
		break;
	case SNDRV_PCM_TRIGGER_STOP:
	case SNDRV_PCM_TRIGGER_SUSPEND:
	case SNDRV_PCM_TRIGGER_PAUSE_PUSH:
		ac108_set_bclk_en(priv, 0, false);
		break;
	default:
		return -EINVAL;
	}
	return 0;
}

static const struct snd_soc_dai_ops ac108_dai_ops = {
	.set_fmt	= ac108_set_fmt,
	.hw_params	= ac108_hw_params,
	.hw_free	= ac108_hw_free,
	.trigger	= ac108_trigger,
};

static struct snd_soc_dai_driver ac108_dai = {
	.name = "ac108-pcm",
	.capture = {
		.stream_name	= "Capture",
		.channels_min	= 2,
		.channels_max	= 8,
		.rates		= AC108_RATES,
		.formats	= AC108_FORMATS,
	},
	.ops = &ac108_dai_ops,
};

static int ac108_component_probe(struct snd_soc_component *component)
{
	struct ac108_priv *priv = snd_soc_component_get_drvdata(component);
	struct device *dev = component->dev;
	struct snd_kcontrol_new *kctl;
	struct ac108_ctl *ctl;
	struct ac108_chip *c;
	int i, j, n;

	n = priv->num_chips * AC108_CTLS_PER_CHIP;
	kctl = devm_kcalloc(dev, n, sizeof(*kctl), GFP_KERNEL);
	ctl = devm_kcalloc(dev, n, sizeof(*ctl), GFP_KERNEL);
	if (!kctl || !ctl)
		return -ENOMEM;

	for_each_chip(priv, i, c) {
		/* Reset all registers to their defaults, then power up */
		regmap_write(c->regmap, CHIP_AUDIO_RST, 0x12);
		usleep_range(1000, 2000);
		ac108_hw_init(c->regmap);

		for (j = 0; j < AC108_CTLS_PER_CHIP; j++) {
			int k = i * AC108_CTLS_PER_CHIP + j;

			ctl[k].regmap = c->regmap;
			ctl[k].reg = ac108_ctl_tmpl[j].reg;
			ctl[k].shift = ac108_ctl_tmpl[j].shift;
			ctl[k].max = ac108_ctl_tmpl[j].max;

			kctl[k].iface = SNDRV_CTL_ELEM_IFACE_MIXER;
			kctl[k].name = devm_kasprintf(dev, GFP_KERNEL, ac108_ctl_tmpl[j].fmt,
						      c->channel_base + j % 4 + 1);
			if (!kctl[k].name)
				return -ENOMEM;
			kctl[k].access = SNDRV_CTL_ELEM_ACCESS_READWRITE |
					 SNDRV_CTL_ELEM_ACCESS_TLV_READ;
			kctl[k].tlv.p = ac108_ctl_tmpl[j].tlv;
			kctl[k].info = ac108_ctl_info;
			kctl[k].get = ac108_ctl_get;
			kctl[k].put = ac108_ctl_put;
			kctl[k].private_value = (unsigned long)&ctl[k];
		}
	}
	return snd_soc_add_component_controls(component, kctl, n);
}

static int ac108_component_resume(struct snd_soc_component *component)
{
	struct ac108_priv *priv = snd_soc_component_get_drvdata(component);
	struct ac108_chip *c;
	int i;

	for_each_chip(priv, i, c)
		ac108_hw_init(c->regmap);
	return 0;
}

static const struct snd_soc_component_driver ac108_component = {
	.probe		= ac108_component_probe,
	.resume		= ac108_component_resume,
	.endianness	= 1,
};

static const struct regmap_config ac108_regmap_config = {
	.reg_bits	= 8,
	.val_bits	= 8,
	.max_register	= 0xff,
};

static void ac108_put_device(void *data)
{
	put_device(data);
}

/* Collect the secondary chips; defer until they have all probed. */
static int ac108_get_secondaries(struct device *dev, struct ac108_priv *priv)
{
	int i, n, ret;

	n = of_count_phandle_with_args(dev->of_node, "x-powers,secondary-chips", NULL);
	if (n <= 0)
		return 0;
	if (n >= AC108_MAX_CHIPS)
		return dev_err_probe(dev, -EINVAL, "too many secondary chips\n");

	for (i = 0; i < n; i++) {
		struct device_node *np;
		struct i2c_client *client;
		struct ac108_chip *chip;

		np = of_parse_phandle(dev->of_node, "x-powers,secondary-chips", i);
		if (!np)
			return -EINVAL;
		client = of_find_i2c_device_by_node(np);
		of_node_put(np);
		if (!client)
			return dev_err_probe(dev, -EPROBE_DEFER, "secondary chip %d not found\n", i);

		ret = devm_add_action_or_reset(dev, ac108_put_device, &client->dev);
		if (ret)
			return ret;

		/* Set at the end of the secondary's probe */
		chip = i2c_get_clientdata(client);
		if (!chip || !client->dev.driver)
			return dev_err_probe(dev, -EPROBE_DEFER,
					     "secondary chip %s not ready\n", dev_name(&client->dev));

		/* Unbind the primary before any of its secondaries */
		if (!device_link_add(dev, &client->dev, DL_FLAG_AUTOREMOVE_CONSUMER))
			return dev_err_probe(dev, -EINVAL, "cannot link to %s\n",
					     dev_name(&client->dev));

		priv->chips[priv->num_chips++] = chip;
	}
	return 0;
}

static int ac108_i2c_probe(struct i2c_client *i2c)
{
	struct device *dev = &i2c->dev;
	struct ac108_chip *chip;
	struct ac108_priv *priv;
	unsigned int val;
	int ret;

	chip = devm_kzalloc(dev, sizeof(*chip), GFP_KERNEL);
	if (!chip)
		return -ENOMEM;

	chip->regmap = devm_regmap_init_i2c(i2c, &ac108_regmap_config);
	if (IS_ERR(chip->regmap))
		return PTR_ERR(chip->regmap);

	ret = regmap_read(chip->regmap, CHIP_AUDIO_RST, &val);
	if (ret)
		return dev_err_probe(dev, ret, "chip not responding\n");

	chip->bclk_en = devm_gpiod_get_optional(dev, "bclk-en", GPIOD_OUT_LOW);
	if (IS_ERR(chip->bclk_en))
		return dev_err_probe(dev, PTR_ERR(chip->bclk_en),
				     "failed to get bclk-en GPIO\n");

	of_property_read_u32(dev->of_node, "x-powers,channel-base",
			     &chip->channel_base);

	/* A secondary chip is driven by its primary: nothing to register */
	if (!of_property_present(dev->of_node, "#sound-dai-cells")) {
		i2c_set_clientdata(i2c, chip);
		return 0;
	}

	priv = devm_kzalloc(dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;
	priv->chips[priv->num_chips++] = chip;

	ret = ac108_get_secondaries(dev, priv);
	if (ret)
		return ret;

	/* The primary's drvdata is the whole group (the component's view) */
	dev_set_drvdata(dev, priv);
	return devm_snd_soc_register_component(dev, &ac108_component, &ac108_dai, 1);
}

static const struct of_device_id ac108_of_match[] = {
	{ .compatible = "x-powers,ac108" },
	{ }
};
MODULE_DEVICE_TABLE(of, ac108_of_match);

static const struct i2c_device_id ac108_i2c_id[] = {
	{ "ac108" },
	{ }
};
MODULE_DEVICE_TABLE(i2c, ac108_i2c_id);

static struct i2c_driver ac108_i2c_driver = {
	.driver = {
		.name = "ac108",
		.of_match_table = ac108_of_match,
	},
	.probe = ac108_i2c_probe,
	.id_table = ac108_i2c_id,
};
module_i2c_driver(ac108_i2c_driver);

MODULE_DESCRIPTION("X-Powers AC108 ADC driver for the ReSpeaker Core v2");
MODULE_AUTHOR("panjunwen");
MODULE_LICENSE("GPL");
