#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <x264.h>

static double now_seconds(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static double cpu_seconds(void)
{
    struct timespec t;
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t);
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static FILE *open_suffix(const char *base, const char *suffix)
{
    size_t length = strlen(base) + strlen(suffix) + 1;
    char *name = malloc(length);
    if (!name)
        return NULL;
    snprintf(name, length, "%s%s", base, suffix);
    FILE *file = fopen(name, "wb");
    if (!file)
        perror(name);
    free(name);
    return file;
}

static int write_annexb(FILE *file, const uint8_t *avcc, int bytes)
{
    int offset = 0;
    const uint8_t start[4] = {0, 0, 0, 1};
    while (offset < bytes)
    {
        if (bytes - offset < 4)
            return -1;
        uint32_t size = ((uint32_t)avcc[offset] << 24) |
                        ((uint32_t)avcc[offset + 1] << 16) |
                        ((uint32_t)avcc[offset + 2] << 8) | avcc[offset + 3];
        offset += 4;
        if (!size || size > (uint32_t)(bytes - offset))
            return -1;
        if (fwrite(start, 1, 4, file) != 4 ||
            fwrite(avcc + offset, 1, size, file) != size)
            return -1;
        offset += (int)size;
    }
    return 0;
}

struct output_state
{
    FILE *packets, *index, *annexb;
    uint8_t *sei;
    int sei_bytes, packet_count, central_count, future_count;
    int central_start, central_frames;
    uint8_t *central_seen;
    int64_t offset;
};

static int packet(struct output_state *state, x264_nal_t *nals, int count,
                  x264_picture_t *picture, int size)
{
    if (!size)
        return 0;
    int prefix = state->packet_count == 0 ? state->sei_bytes : 0;
    if (prefix && (fwrite(state->sei, 1, prefix, state->packets) != (size_t)prefix ||
                   write_annexb(state->annexb, state->sei, prefix)))
        return -1;
    int actual_bytes = prefix;
    for (int i = 0; i < count; i++)
    {
        if (fwrite(nals[i].p_payload, 1, nals[i].i_payload, state->packets) !=
                (size_t)nals[i].i_payload ||
            write_annexb(state->annexb, nals[i].p_payload, nals[i].i_payload))
            return -1;
        actual_bytes += nals[i].i_payload;
    }
    if (actual_bytes != size + prefix)
        return -1;
    fprintf(state->index,
            "{\"pts\":%" PRId64 ",\"dts\":%" PRId64
            ",\"offset\":%" PRId64 ",\"size\":%d,\"type\":%d,\"keyframe\":%d,\"nal_types\":[",
            picture->i_pts, picture->i_dts, state->offset, actual_bytes,
            picture->i_type, picture->b_keyframe);
    for (int i = 0; i < count; i++)
        fprintf(state->index, "%s%d", i ? "," : "", nals[i].i_type);
    fputs("]}\n", state->index);
    state->offset += actual_bytes;
    state->packet_count++;
    int64_t position = picture->i_pts - state->central_start;
    if (position >= 0 && position < state->central_frames)
    {
        if (state->central_seen[position])
            return -1;
        state->central_seen[position] = 1;
        state->central_count++;
    }
    else if (position >= state->central_frames)
        state->future_count++;
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 11)
    {
        fprintf(stderr, "usage: helper W H FPS_NUM FPS_DEN TOTAL CENTRAL_START CENTRAL_FRAMES SAR_NUM SAR_DEN OUTPUT_BASE [stdin rawI420]\n");
        return 2;
    }
    int width = atoi(argv[1]), height = atoi(argv[2]);
    int fps_num = atoi(argv[3]), fps_den = atoi(argv[4]), total = atoi(argv[5]);
    int central_start = atoi(argv[6]), central_frames = atoi(argv[7]);
    int sar_num = atoi(argv[8]), sar_den = atoi(argv[9]);
    const char *base = argv[10];
    const char *mode = getenv("VSIZE_CONTEXT_MODE");
    int stop = mode && strcmp(mode, "stop") == 0;
    if (width <= 0 || height <= 0 || width > 16384 || height > 16384 ||
        width % 2 || height % 2 || fps_num <= 0 || fps_den <= 0 ||
        total <= 0 || total > 10000000 || central_start < 0 ||
        central_frames <= 0 || central_start + central_frames > total)
        return 2;
    double started = now_seconds(), cpu_started = cpu_seconds();
    double encoder_seconds = 0, encoder_cpu_seconds = 0, read_seconds = 0;
    x264_param_t parameters;
    if (x264_param_default_preset(&parameters, "medium", NULL))
        return 3;
    parameters.i_width = width;
    parameters.i_height = height;
    parameters.i_csp = X264_CSP_I420;
    parameters.i_bitdepth = 8;
    parameters.i_threads = 1;
    parameters.i_lookahead_threads = 1;
    parameters.b_annexb = 0;
    parameters.b_repeat_headers = 0;
    parameters.b_vfr_input = 1;
    parameters.i_fps_num = (uint32_t)fps_num;
    parameters.i_fps_den = (uint32_t)fps_den;
    parameters.i_timebase_num = (uint32_t)fps_den;
    parameters.i_timebase_den = (uint32_t)fps_num;
    parameters.vui.i_sar_width = sar_num;
    parameters.vui.i_sar_height = sar_den;
    const char *color_primaries = getenv("VSIZE_VUI_COLOR_PRIMARIES");
    const char *color_transfer = getenv("VSIZE_VUI_COLOR_TRANSFER");
    const char *color_matrix = getenv("VSIZE_VUI_COLOR_MATRIX");
    const char *full_range = getenv("VSIZE_VUI_FULL_RANGE");
    const char *chroma_location = getenv("VSIZE_VUI_CHROMA_LOCATION");
    if (color_primaries)
        parameters.vui.i_colorprim = atoi(color_primaries);
    if (color_transfer)
        parameters.vui.i_transfer = atoi(color_transfer);
    if (color_matrix)
        parameters.vui.i_colmatrix = atoi(color_matrix);
    if (full_range)
        parameters.vui.b_fullrange = atoi(full_range);
    if (chroma_location)
        parameters.vui.i_chroma_loc = atoi(chroma_location);
    parameters.rc.i_rc_method = X264_RC_CRF;
    parameters.rc.f_rf_constant = 23;
    parameters.i_log_level = X264_LOG_INFO;
    x264_t *encoder = x264_encoder_open(&parameters);
    if (!encoder)
        return 3;
    struct output_state output = {0};
    output.central_start = central_start;
    output.central_frames = central_frames;
    output.central_seen = calloc((size_t)central_frames, 1);
    output.packets = open_suffix(base, ".packets.avcc");
    output.index = open_suffix(base, ".packets.jsonl");
    output.annexb = open_suffix(base, ".h264");
    FILE *headers = open_suffix(base, ".headers.avcc");
    x264_picture_t input, picture;
    memset(&input, 0, sizeof(input));
    int allocated = 0, status = 0, fed = 0, flush_calls = 0;
    double setup_seconds = 0, setup_cpu_seconds = 0;
    if (!output.central_seen || !output.packets || !output.index || !output.annexb || !headers)
    {
        status = 4;
        goto cleanup;
    }
    x264_nal_t *nals = NULL;
    int nal_count = 0;
    if (x264_encoder_headers(encoder, &nals, &nal_count) < 0)
    {
        status = 3;
        goto cleanup;
    }
    for (int i = 0; i < nal_count; i++)
    {
        if (fwrite(nals[i].p_payload, 1, nals[i].i_payload, headers) != (size_t)nals[i].i_payload)
        {
            status = 4;
            goto cleanup;
        }
        if (nals[i].i_type == NAL_SEI)
        {
            uint8_t *next = realloc(output.sei, (size_t)output.sei_bytes + nals[i].i_payload);
            if (!next)
            {
                status = 4;
                goto cleanup;
            }
            output.sei = next;
            memcpy(output.sei + output.sei_bytes, nals[i].p_payload, (size_t)nals[i].i_payload);
            output.sei_bytes += nals[i].i_payload;
        }
        else if (write_annexb(output.annexb, nals[i].p_payload, nals[i].i_payload))
        {
            status = 4;
            goto cleanup;
        }
    }
    if (x264_picture_alloc(&input, X264_CSP_I420, width, height))
    {
        status = 4;
        goto cleanup;
    }
    allocated = 1;
    setup_seconds = now_seconds() - started;
    setup_cpu_seconds = cpu_seconds() - cpu_started;
    for (int frame = 0; frame < total; frame++)
    {
        double read_start = now_seconds();
        for (int plane = 0; plane < 3; plane++)
        {
            int rows = plane ? height / 2 : height;
            int row_bytes = plane ? width / 2 : width;
            for (int row = 0; row < rows; row++)
                if (fread(input.img.plane[plane] + row * input.img.i_stride[plane],
                          1, (size_t)row_bytes, stdin) != (size_t)row_bytes)
                {
                    status = 5;
                    goto cleanup;
                }
        }
        read_seconds += now_seconds() - read_start;
        input.i_pts = frame;
        input.i_type = X264_TYPE_AUTO;
        input.i_qpplus1 = X264_QP_AUTO;
        double encode_start = now_seconds();
        double encode_cpu_start = cpu_seconds();
        int bytes = x264_encoder_encode(encoder, &nals, &nal_count, &input, &picture);
        encoder_seconds += now_seconds() - encode_start;
        encoder_cpu_seconds += cpu_seconds() - encode_cpu_start;
        fed++;
        if (bytes < 0 || packet(&output, nals, nal_count, &picture, bytes))
        {
            status = 3;
            goto cleanup;
        }
        if (stop && output.central_count == central_frames)
            break;
    }
    if (!stop)
        while (x264_encoder_delayed_frames(encoder))
        {
            double encode_start = now_seconds();
            double encode_cpu_start = cpu_seconds();
            int bytes = x264_encoder_encode(encoder, &nals, &nal_count, NULL, &picture);
            encoder_seconds += now_seconds() - encode_start;
            encoder_cpu_seconds += cpu_seconds() - encode_cpu_start;
            flush_calls++;
            if (bytes < 0 || packet(&output, nals, nal_count, &picture, bytes))
            {
                status = 3;
                goto cleanup;
            }
        }
    if (output.central_count != central_frames)
        status = 6;
cleanup:
    {
        int queued = x264_encoder_delayed_frames(encoder);
        x264_encoder_parameters(encoder, &parameters);
        double close_start = now_seconds();
        double close_cpu_start = cpu_seconds();
        x264_encoder_close(encoder);
        encoder_seconds += now_seconds() - close_start;
        encoder_cpu_seconds += cpu_seconds() - close_cpu_start;
        if (allocated)
            x264_picture_clean(&input);
        if (output.packets)
            fclose(output.packets);
        if (output.index)
            fclose(output.index);
        if (output.annexb)
            fclose(output.annexb);
        if (headers)
            fclose(headers);
        free(output.sei);
        free(output.central_seen);
        printf("{\"status\":%d,\"mode\":\"%s\",\"fed_frames\":%d,"
               "\"produced_packets\":%d,\"central_packets\":%d,\"future_packets\":%d,"
               "\"queued_frames_discarded\":%d,\"flush_calls\":%d,"
               "\"encoder_seconds\":%.9f,\"stdin_read_seconds\":%.9f,"
               "\"encoder_cpu_seconds\":%.9f,\"process_cpu_seconds\":%.9f,"
               "\"setup_seconds\":%.9f,\"setup_cpu_seconds\":%.9f,"
               "\"process_seconds\":%.9f,\"global_sei_bytes\":%d,"
               "\"lookahead\":%d,\"bframes\":%d,\"vfr\":%d,\"annexb\":%d,"
               "\"repeat_headers\":%d,\"keyint\":%d,\"keyint_min\":%d}\n",
               status, stop ? "stop" : "full", fed, output.packet_count,
               output.central_count, output.future_count, queued, flush_calls,
               encoder_seconds, read_seconds, encoder_cpu_seconds, cpu_seconds() - cpu_started,
               setup_seconds, setup_cpu_seconds, now_seconds() - started,
               output.sei_bytes, parameters.rc.i_lookahead, parameters.i_bframe,
               parameters.b_vfr_input, parameters.b_annexb, parameters.b_repeat_headers,
               parameters.i_keyint_max, parameters.i_keyint_min);
    }
    return status;
}
