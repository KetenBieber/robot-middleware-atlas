#define _POSIX_C_SOURCE 200809L

#include "virtual_servo.h"

#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    unsigned int control_word;
    unsigned int target_torque;
    unsigned int status_word;
    unsigned int position;
    unsigned int velocity;
} atlas_offsets_t;

static void add_ns(struct timespec *t, long ns)
{
    t->tv_nsec += ns;
    while (t->tv_nsec >= 1000000000L) {
        t->tv_nsec -= 1000000000L;
        ++t->tv_sec;
    }
}

static int32_t to_i32(double value, double scale)
{
    const double scaled = value * scale;
    if (scaled > 2147483647.0) return INT32_MAX;
    if (scaled < -2147483648.0) return INT32_MIN;
    return (int32_t) llround(scaled);
}

int main(int argc, char **argv)
{
    const long cycles = argc > 1 ? strtol(argv[1], NULL, 10) : 8000;
    if (cycles <= 0) {
        fprintf(stderr, "cycles must be positive\n");
        return 2;
    }

    ec_master_t *master = ecrt_request_master(0);
    if (!master) {
        fprintf(stderr, "plant: ecrt_request_master(0) failed\n");
        return 3;
    }

    ec_domain_t *domain = ecrt_master_create_domain(master);
    if (!domain) {
        fprintf(stderr, "plant: ecrt_master_create_domain() failed\n");
        ecrt_release_master(master);
        return 4;
    }

    ec_slave_config_t *sc = ecrt_master_slave_config(
        master,
        ATLAS_SERVO_ALIAS,
        ATLAS_SERVO_POSITION,
        ATLAS_SERVO_VENDOR_ID,
        ATLAS_SERVO_PRODUCT_CODE);
    if (!sc) {
        fprintf(stderr, "plant: ecrt_master_slave_config() failed\n");
        ecrt_release_master(master);
        return 5;
    }

    if (ecrt_slave_config_pdos(sc, EC_END, atlas_plant_syncs)) {
        fprintf(stderr, "plant: ecrt_slave_config_pdos() failed\n");
        ecrt_release_master(master);
        return 6;
    }

    atlas_offsets_t off = {0};
    const ec_pdo_entry_reg_t regs[] = {
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6040, 0x00, &off.control_word},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6071, 0x00, &off.target_torque},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6041, 0x00, &off.status_word},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6064, 0x00, &off.position},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x606C, 0x00, &off.velocity},
        {}
    };

    if (ecrt_domain_reg_pdo_entry_list(domain, regs)) {
        fprintf(stderr, "plant: PDO registration failed\n");
        ecrt_release_master(master);
        return 7;
    }

    if (ecrt_master_activate(master)) {
        fprintf(stderr, "plant: activate failed\n");
        ecrt_release_master(master);
        return 8;
    }

    uint8_t *pd = ecrt_domain_data(domain);
    if (!pd) {
        fprintf(stderr, "plant: domain data is NULL\n");
        ecrt_release_master(master);
        return 9;
    }

    /*
     * Synthetic one-axis dynamics:
     *     J*qdd + b*qd = tau
     * This process is only for exercising PDO direction, process-image
     * offsets and cyclic exchange. It is not a motor/drive model.
     */
    const double dt = 0.001;
    const double inertia = 0.05;
    const double damping = 0.22;
    double position = 0.0;
    double velocity = 0.0;

    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);

    for (long k = 0; k < cycles; ++k) {
        add_ns(&next, ATLAS_PERIOD_NS);
        const int sleep_rc = clock_nanosleep(
            CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
        if (sleep_rc && sleep_rc != EINTR) {
            fprintf(stderr, "plant clock_nanosleep: %s\n", strerror(sleep_rc));
            break;
        }

        ecrt_master_receive(master);
        ecrt_domain_process(domain);

        const uint16_t control_word = EC_READ_U16(pd + off.control_word);
        const int16_t target_torque = EC_READ_S16(pd + off.target_torque);
        const double torque_nm = target_torque / 1000.0;

        const double acceleration =
            (torque_nm - damping * velocity) / inertia;
        velocity += acceleration * dt;
        position += velocity * dt;

        const uint16_t synthetic_status =
            control_word ? 0x0027u : 0x0040u;

        EC_WRITE_U16(pd + off.status_word, synthetic_status);
        EC_WRITE_S32(pd + off.position,
                     to_i32(position, ATLAS_POSITION_SCALE));
        EC_WRITE_S32(pd + off.velocity,
                     to_i32(velocity, ATLAS_VELOCITY_SCALE));

        if (k % 500 == 0) {
            printf("plant k=%ld pos=%+.4f vel=%+.4f tau=%+.3f\n",
                   k, position, velocity, torque_nm);
        }

        ecrt_domain_queue(domain);
        ecrt_master_send(master);
    }

    ecrt_release_master(master);
    return 0;
}
