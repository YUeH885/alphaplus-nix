// SPDX-License-Identifier: MIT
#include <stdint.h>

union remote_arg {
	struct {
		void *data;
		uint32_t size;
	} buffer;
	uint64_t handle;
};

int alphaplus_cdsp_skel_handle_invoke(uint64_t handle, uint32_t scalar,
				  union remote_arg *args)
{
	const uint32_t *parameters;
	const uint32_t *left, *right;
	uint32_t *output;
	uint32_t count, bytes;

	(void)handle;
	/* The module loader reserves methods 0 and 1 for handle lifetime. */
	if (scalar == 0x00020001) {
		args[2].handle = 1;
		return 0;
	}
	if (scalar == 0x01000010)
		return 0;
	if (scalar != 0x02030100 || args[0].buffer.size != 16)
		return 14;
	parameters = args[0].buffer.data;
	count = parameters[0];
	if (count > UINT32_MAX / sizeof(uint32_t))
		return 14;
	bytes = count * sizeof(uint32_t);
	for (unsigned int i = 1; i < 4; i++) {
		if (parameters[i] != bytes || args[i].buffer.size < bytes)
			return 14;
	}
	left = args[1].buffer.data;
	right = args[2].buffer.data;
	output = args[3].buffer.data;
	for (uint32_t i = 0; i < count; i++)
		output[i] = left[i] + right[i];
	return 0;
}
