/** @type {import('@commitlint/types').UserConfig} */
export default {
    defaultIgnores: true,
    // CI sets this only from the trusted pull request author context.
    ignores: [
        // The pull_request merge ref has a synthetic "Merge remote branch" commit; validate the authored PR commits.
        (message) => message.split('\n', 1)[0] === 'Merge remote branch',
        (message) =>
            process.env.DEPENDABOT_PR === 'true' &&
            /^(?:chore\((?:deps|deps-dev)\)|ci\(deps\)): (?:[Bb]ump\s+|lockfile maintenance)/u.test(message),
    ],
    rules: {
        'header-max-length': [2, 'always', 120],
        'header-min-length': [2, 'always', 10],
        'header-full-stop': [2, 'never', '.'],
        'type-enum': [
            2,
            'always',
            [
                'feat',
                'fix',
                'docs',
                'style',
                'refactor',
                'perf',
                'test',
                'build',
                'ci',
                'chore',
                'revert',
            ],
        ],
        'type-empty': [2, 'never'],
        'subject-empty': [2, 'never'],
        'type-case': [2, 'always', 'lower-case'],
        'subject-case': [2, 'always', ['sentence-case']],
    },
};
