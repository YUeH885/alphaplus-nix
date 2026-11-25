// SPDX-License-Identifier: GPL-2.0
#include <errno.h>
#include <fcntl.h>
#include <linux/qcom_camss.h>
#include <linux/videodev2.h>
#include <stdbool.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

_Static_assert(sizeof(struct v4l2_qcom_camss_ife_config) == 472,
	       "unexpected IFE configuration ABI size");

static int set_config(int fd, struct v4l2_qcom_camss_ife_config *config)
{
	struct v4l2_ext_control control = {
		.id = V4L2_CID_QCOM_CAMSS_IFE_CONFIG,
		.size = sizeof(*config),
		.ptr = (void *)config,
	};
	struct v4l2_ext_controls controls = {
		.which = V4L2_CTRL_WHICH_CUR_VAL,
		.count = 1,
		.request_fd = -1,
		.controls = &control,
	};

	return ioctl(fd, VIDIOC_S_EXT_CTRLS, &controls);
}

static void prepare_config(struct v4l2_qcom_camss_ife_config *config,
			   uint32_t sequence, uint32_t flag,
			   const char *profile)
{
	unsigned int i;
	bool flat = !strcmp(profile, "flat");

	memset(config, 0, sizeof(*config));
	config->version = V4L2_QCOM_CAMSS_IFE_CONFIG_VERSION;
	config->frame_sequence = sequence;
	config->flags = flag;

	if (flag == V4L2_QCOM_CAMSS_IFE_CONFIG_BLACK_LEVEL) {
		config->black_level = !strcmp(profile, "no-bls") || flat ? 0 : 1024;
	} else if (flag == V4L2_QCOM_CAMSS_IFE_CONFIG_WHITE_BALANCE) {
		if (!strcmp(profile, "wb-red")) {
			config->wb_gain_q7[0] = 256;
			config->wb_gain_q7[1] = 128;
			config->wb_gain_q7[2] = 128;
			config->wb_gain_q7[3] = 128;
		} else if (!strcmp(profile, "wb-blue")) {
			config->wb_gain_q7[0] = 128;
			config->wb_gain_q7[1] = 128;
			config->wb_gain_q7[2] = 128;
			config->wb_gain_q7[3] = 256;
		} else {
			for (i = 0; i < 4; i++)
				config->wb_gain_q7[i] = 128;
		}
		if (!strcmp(profile, "offsets")) {
			config->wb_offset[0] = -1;
			config->wb_offset[1] = -2;
			config->wb_offset[2] = -2;
			config->wb_offset[3] = -3;
		}
	} else if (flag == V4L2_QCOM_CAMSS_IFE_CONFIG_CCM) {
		static const int16_t factory_ccm[9] = {
			214, -93, 7, -38, 170, -5, 6, -157, 279,
		};
		static const int16_t swap_ccm[9] = {
			0, 0, 128, 0, 128, 0, 128, 0, 0,
		};

		if (flat || !strcmp(profile, "ccm-flat")) {
			config->ccm_q7[0] = 128;
			config->ccm_q7[4] = 128;
			config->ccm_q7[8] = 128;
		} else if (!strcmp(profile, "ccm-swap")) {
			memcpy(config->ccm_q7, swap_ccm, sizeof(swap_ccm));
		} else {
			memcpy(config->ccm_q7, factory_ccm,
			       sizeof(factory_ccm));
		}
		if (!strcmp(profile, "offsets")) {
			config->ccm_offset[0] = -4;
			config->ccm_offset[1] = -5;
			config->ccm_offset[2] = -6;
		}
	} else if (flag == V4L2_QCOM_CAMSS_IFE_CONFIG_GAMMA) {
		static const uint16_t gamma[V4L2_QCOM_CAMSS_IFE_GAMMA_ENTRIES] = {
			0, 64, 108, 144, 176, 205, 232, 258,
			282, 304, 326, 347, 367, 386, 405, 423,
			441, 458, 475, 491, 507, 523, 538, 553,
			568, 583, 597, 611, 625, 638, 651, 665,
			678, 690, 703, 715, 728, 740, 752, 764,
			775, 787, 798, 810, 821, 832, 843, 854,
			865, 875, 886, 896, 907, 917, 927, 937,
			947, 957, 967, 977, 987, 996, 1006, 1015,
			1023,
		};
		unsigned int span = V4L2_QCOM_CAMSS_IFE_GAMMA_ENTRIES - 1;

		for (i = 0; i < V4L2_QCOM_CAMSS_IFE_GAMMA_ENTRIES; i++) {
			uint32_t value = gamma[i];

			if (flat || !strcmp(profile, "no-gamma"))
				value = i * 1023 / span;
			else if (!strcmp(profile, "gamma-dark"))
				value = i * i * 1023 / (span * span);
			else if (!strcmp(profile, "gamma-bright"))
				value = 1023 - (span - i) * (span - i) *
					1023 / (span * span);
			config->gamma[0][i] = value;
			config->gamma[1][i] = value;
			config->gamma[2][i] = value;
		}
	}
}

int main(int argc, char **argv)
{
	static const uint32_t flags[] = {
		V4L2_QCOM_CAMSS_IFE_CONFIG_BLACK_LEVEL,
		V4L2_QCOM_CAMSS_IFE_CONFIG_WHITE_BALANCE,
		V4L2_QCOM_CAMSS_IFE_CONFIG_CCM,
		V4L2_QCOM_CAMSS_IFE_CONFIG_GAMMA,
	};
	struct v4l2_event_subscription subscription = {
		.type = V4L2_EVENT_FRAME_SYNC,
	};
	struct v4l2_qcom_camss_ife_config config;
	struct pollfd pollfd;
	char *end;
	unsigned int frame_limit = 120;
	unsigned int timeouts = 0;
	unsigned int applied = 0;
	int fd;

	if (argc < 2 || argc > 4) {
		fprintf(stderr,
			"Usage: %s VFE_SUBDEV [FRAME_LIMIT] "
			"[factory|flat|no-bls|no-gamma|gamma-dark|"
			"gamma-bright|gamma-alternate|wb-red|wb-blue|"
			"wb-alternate|ccm-flat|ccm-swap|ccm-alternate|offsets]\n",
			argv[0]);
		return EXIT_FAILURE;
	}
	if (argc >= 3) {
		unsigned long value = strtoul(argv[2], &end, 10);

		if (!*argv[2] || *end || !value || value > UINT32_MAX) {
			fprintf(stderr, "Invalid frame limit: %s\n", argv[2]);
			return EXIT_FAILURE;
		}
		frame_limit = value;
	}
	if (argc == 4 && strcmp(argv[3], "factory") &&
	    strcmp(argv[3], "flat") && strcmp(argv[3], "no-bls") &&
	    strcmp(argv[3], "no-gamma") && strcmp(argv[3], "gamma-dark") &&
	    strcmp(argv[3], "gamma-bright") &&
	    strcmp(argv[3], "gamma-alternate") &&
	    strcmp(argv[3], "wb-red") && strcmp(argv[3], "wb-blue") &&
	    strcmp(argv[3], "wb-alternate") &&
	    strcmp(argv[3], "ccm-flat") && strcmp(argv[3], "ccm-swap") &&
	    strcmp(argv[3], "ccm-alternate") && strcmp(argv[3], "offsets")) {
		fprintf(stderr, "Invalid tuning profile: %s\n", argv[3]);
		return EXIT_FAILURE;
	}

	fd = open(argv[1], O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		perror("open VFE subdevice");
		return EXIT_FAILURE;
	}
	if (ioctl(fd, VIDIOC_SUBSCRIBE_EVENT, &subscription) < 0) {
		perror("VIDIOC_SUBSCRIBE_EVENT");
		close(fd);
		return EXIT_FAILURE;
	}

	pollfd.fd = fd;
	pollfd.events = POLLPRI;

	if (argc == 4 && (!strcmp(argv[3], "gamma-alternate") ||
			  !strcmp(argv[3], "wb-alternate") ||
			  !strcmp(argv[3], "ccm-alternate"))) {
		uint32_t target_flag = V4L2_QCOM_CAMSS_IFE_CONFIG_GAMMA;
		const char *mode0 = "gamma-bright";
		const char *mode1 = "gamma-dark";
		const char *tag = "gamma";
		unsigned int updates = 0;
		unsigned int attempts = 0;
		unsigned int timeouts = 0;

		if (!strcmp(argv[3], "wb-alternate")) {
			target_flag = V4L2_QCOM_CAMSS_IFE_CONFIG_WHITE_BALANCE;
			mode0 = "wb-red";
			mode1 = "wb-blue";
			tag = "wb";
		} else if (!strcmp(argv[3], "ccm-alternate")) {
			target_flag = V4L2_QCOM_CAMSS_IFE_CONFIG_CCM;
			mode0 = "factory";
			mode1 = "ccm-swap";
			tag = "ccm";
		}

		while (updates < frame_limit) {
			struct v4l2_event event;
			const char *current_profile;
			int ret;

			if (++attempts > frame_limit * 4 + 50) {
				fprintf(stderr, "Timed out alternating %s updates\n", tag);
				goto fail;
			}
			ret = poll(&pollfd, 1, 1000);
			if (ret < 0 && errno == EINTR)
				continue;
			if (ret < 0) {
				perror("poll frame sync");
				goto fail;
			}
			if (!ret) {
				if (++timeouts >= 5)
					break;
				continue;
			}
			timeouts = 0;
			if (ioctl(fd, VIDIOC_DQEVENT, &event) < 0) {
				perror("VIDIOC_DQEVENT");
				goto fail;
			}

			current_profile = (updates & 1) ? mode1 : mode0;
			prepare_config(&config,
				       event.u.frame_sync.frame_sequence + 1,
				       target_flag, current_profile);
			if (set_config(fd, &config) < 0) {
				if (errno == EAGAIN)
					continue;
				perror("VIDIOC_S_EXT_CTRLS");
				goto fail;
			}
			printf("%s %s target %u\n", tag, current_profile,
			       event.u.frame_sync.frame_sequence + 1);
			updates++;
		}
		if (!updates) {
			fprintf(stderr, "No %s updates accepted\n", tag);
			goto fail;
		}
		printf("PASS: %u alternating %s updates accepted\n", updates, tag);
		close(fd);
		return EXIT_SUCCESS;
	}

	while (applied != V4L2_QCOM_CAMSS_IFE_CONFIG_ALL && frame_limit--) {
		struct v4l2_event event;
		unsigned int index;
		int ret;

		ret = poll(&pollfd, 1, 1000);
		if (ret < 0 && errno == EINTR)
			continue;
		if (ret < 0) {
			perror("poll frame sync");
			goto fail;
		}
		if (!ret) {
			if (++timeouts >= 5) {
				fprintf(stderr, "Timed out waiting for frame sync events\n");
				goto fail;
			}
			continue;
		}
		timeouts = 0;
		if (ioctl(fd, VIDIOC_DQEVENT, &event) < 0) {
			perror("VIDIOC_DQEVENT");
			goto fail;
		}

		for (index = 0; index < sizeof(flags) / sizeof(flags[0]); index++)
			if (!(applied & flags[index]))
				break;
		prepare_config(&config, event.u.frame_sync.frame_sequence + 1,
		       flags[index], argc == 4 ? argv[3] : "factory");
		if (set_config(fd, &config) < 0) {
			if (errno == EAGAIN)
				continue;
			perror("VIDIOC_S_EXT_CTRLS");
			goto fail;
		}
		applied |= flags[index];
	}

	if (applied != V4L2_QCOM_CAMSS_IFE_CONFIG_ALL) {
		fprintf(stderr, "Timed out applying IFE tuning blocks: 0x%x\n",
			applied);
		goto fail;
	}

	printf("PASS: BLS, white balance, CCM and gamma accepted at frame boundaries\n");
	close(fd);
	return EXIT_SUCCESS;

fail:
	close(fd);
	return EXIT_FAILURE;
}
